"""Background monitor for "trail down" re-entry limits.

A "Trail down" re-entry (see positions.re_enter_from_snapshot) rests a plain
BUY LIMIT at ``trail_down_percent`` below the live price. This loop chases the
price DOWN: whenever the stock falls far enough that ``price × (1 - pct/100)``
is below the order's current limit, it re-prices the limit lower (in place, via
the broker's atomic replace). It only ever moves the limit DOWN — never up — so
you keep chasing a cheaper entry and a bounce back into the resting limit fills.

Managed, not native: the broker sees an ordinary limit order; the ratchet is
ours. Stock-only (we re-price off the stock quote). Same single-process daemon
design as retry_scheduler — start it in exactly one process (see main.py).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 15
BATCH_SIZE = 100
# Statuses where the limit is still working at the broker and worth re-pricing.
_WORKING = (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
# Don't churn the broker for sub-cent moves.
_MIN_STEP = Decimal("0.01")

_LAST_HEARTBEAT: dict[str, datetime] = {}


def _reprice_one(order_id: uuid.UUID) -> str:
    """Re-price a single trail-down order if the stock has dropped enough.
    Returns a short outcome string for logging. Own session per call."""
    with SessionLocal() as db:
        o = db.get(Order, order_id)
        if o is None:
            return "gone"
        # Re-check under the fresh session: still a working, tagged buy limit?
        if (
            o.trail_down_percent is None
            or o.side != OrderSide.BUY
            or o.order_type != OrderType.LIMIT
            or o.status not in _WORKING
            or not o.broker_order_id
            or o.broker_account_id is None
            or o.instrument_type != InstrumentType.STOCK
            or o.limit_price is None
        ):
            return "skip"

        acct = db.get(BrokerAccount, o.broker_account_id)
        if acct is None or acct.connection_status != "connected":
            return "no-broker"
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
        except Exception:  # noqa: BLE001
            return "adapter-fail"

        fn = getattr(adapter, "get_stock_latest_price", None)
        if fn is None:
            return "no-quote-fn"
        try:
            live = fn(o.symbol)
        except Exception:  # noqa: BLE001
            return "quote-fail"
        if live is None or live <= 0:
            return "no-quote"

        target = (Decimal(str(live)) * (Decimal(1) - o.trail_down_percent / Decimal(100))).quantize(_MIN_STEP)
        # Ratchet DOWN only: never raise the limit, and ignore sub-cent moves.
        if target >= o.limit_price - _MIN_STEP / Decimal(2) or target <= 0:
            return "hold"

        req = BrokerOrderRequest(
            instrument_type=InstrumentType.STOCK,
            symbol=o.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=o.quantity,
            limit_price=target,
        )
        try:
            res = adapter.replace_order(o.broker_order_id, req)
        except Exception as exc:  # noqa: BLE001
            # Order may have just filled/canceled — leave it for fills_sync.
            log.info("trail_down: replace failed order=%s: %s", order_id, str(exc)[:200])
            return "replace-fail"

        old = o.limit_price
        o.broker_order_id = res.broker_order_id or o.broker_order_id
        o.limit_price = target
        if res.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
            o.status = res.status
        db.commit()
        return f"repriced {old}->{target}"


def poll_loop(shutdown_check=None) -> None:
    """Long-running loop. Every POLL_INTERVAL_SEC, re-prices working trail-down
    buy limits that the market has fallen past."""
    log.info("trail_down_monitor: starting (interval=%ss)", POLL_INTERVAL_SEC)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("trail_down_monitor: shutdown requested, exiting")
            return
        _LAST_HEARTBEAT["at"] = datetime.now(timezone.utc)
        try:
            with SessionLocal() as db:
                ids = list(db.execute(
                    select(Order.id).where(
                        Order.trail_down_percent.is_not(None),
                        Order.side == OrderSide.BUY,
                        Order.order_type == OrderType.LIMIT,
                        Order.status.in_(_WORKING),
                        Order.broker_order_id.is_not(None),
                    ).limit(BATCH_SIZE)
                ).scalars())
            for oid in ids:
                try:
                    outcome = _reprice_one(oid)
                    if outcome.startswith("repriced"):
                        log.info("trail_down_monitor: order=%s %s", oid, outcome)
                except Exception:  # noqa: BLE001
                    log.exception("trail_down_monitor: error on order=%s", oid)
        except Exception:  # noqa: BLE001
            log.exception("trail_down_monitor: poll iteration failed")

        try:
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("trail_down_monitor: KeyboardInterrupt, exiting")
            return
