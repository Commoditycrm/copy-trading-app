"""Alpaca subscriber mirror-order fill reconciler.

Live trade listeners run ONLY for TRADER accounts (services.listeners filters
role==TRADER), and the SnapTrade subscriber reconciler
(snaptrade_listener._run_subscriber_reconciler) covers SnapTrade subscribers —
but a plain ALPACA SUBSCRIBER account has NEITHER a real-time listener NOR
(unless copy_trader_bracket is on) any background fill sync. So a mirror order
the copy engine placed there, once it fills at Alpaca, can stay SUBMITTED/working
in our DB indefinitely: order history shows it as pending AND close-detection
(which reads filled_quantity) misfires.

This is the Alpaca twin of the SnapTrade subscriber reconciler. Every 30s it
finds connected Alpaca subscriber accounts that have working mirror orders and
refreshes ONLY those orders' status from the broker via
``fills_sync._refresh_open_orders`` — the exact refresh the P&L poller already
runs for copy-bracket subscribers. It deliberately does NOT run the activities
feed (so it never creates synthetic orders); it only ever corrects the status /
filled qty / price of orders we already placed. Worker-only, best-effort,
isolated per account so one bad account can't stall the rest.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderStatus
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

_RECONCILE_INTERVAL_S = 30.0
_WORKING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)
_task: "asyncio.Task | None" = None


def start_alpaca_subscriber_reconciler() -> None:
    """Spawn the reconciler loop. Idempotent. Worker-only — call it where the
    other periodic listeners start (so it never runs on the web tier)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("alpaca subscriber reconciler: no running loop; not starting")
        return
    _task = loop.create_task(_run())
    log.info(
        "alpaca subscriber fill reconciler: started (interval=%.0fs)",
        _RECONCILE_INTERVAL_S,
    )


async def stop_alpaca_subscriber_reconciler() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def _run() -> None:
    while True:
        try:
            await asyncio.to_thread(_reconcile_once)
        except asyncio.CancelledError:
            log.info("alpaca subscriber fill reconciler: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("alpaca subscriber fill reconciler: tick failed")
        await asyncio.sleep(_RECONCILE_INTERVAL_S)


def _reconcile_once() -> None:
    """One sweep. An account holding orders with parent_order_id IS NOT NULL is a
    subscriber's mirror account, so that filter alone scopes us to subscribers
    (never a trader's own account). Find connected ALPACA ones with >=1 working
    mirror and refresh those orders' status from the broker. Only accounts with
    pending work are polled, so API use tracks real work."""
    # Local import avoids any import cycle at module load.
    from app.services.fills_sync import _refresh_open_orders  # noqa: PLC0415

    with SessionLocal() as db:
        working_acct_ids = (
            select(Order.broker_account_id)
            .where(
                Order.parent_order_id.is_not(None),
                Order.status.in_(_WORKING_STATUSES),
                Order.broker_order_id.is_not(None),
                Order.broker_account_id.is_not(None),
            )
            .distinct()
        )
        acct_ids = [
            a.id for a in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.broker == BrokerName.ALPACA,
                    BrokerAccount.connection_status == "connected",
                    BrokerAccount.id.in_(working_acct_ids),
                )
            ).scalars()
        ]

    for acct_id in acct_ids:
        try:
            with SessionLocal() as db:
                acct = db.get(BrokerAccount, acct_id)
                if acct is None or acct.connection_status != "connected":
                    continue
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
                _refresh_open_orders(db, acct, adapter)
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("alpaca subscriber reconcile: account %s failed", acct_id)
