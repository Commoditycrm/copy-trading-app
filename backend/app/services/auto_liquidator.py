"""Auto-liquidation — close every open position and cancel every open
order on a subscriber's broker account when their equity floor trips.

Triggered by ``pnl_poller`` when broker-reported equity falls to or
below ``SubscriberSettings.auto_liquidation_limit``. The poller has
already flipped ``copy_enabled`` to False and stamped
``auto_liquidated_at`` before calling us — we just have to flatten the
account.

Best-effort + per-leg-isolated: a failure on one symbol doesn't abort
the rest. The poller catches any exception we raise so a bad
liquidation never corrupts the rest of the tick.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.base import BrokerOrderRequest
from app.models.broker_account import BrokerAccount
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services import audit
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# Statuses still "alive" at the broker — those orders need cancelling
# before we send the close legs, otherwise broker rejects the close as
# duplicate / oversold.
_CANCELLABLE = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def liquidate_subscriber_account(
    db: Session,
    subscriber_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
) -> dict:
    """Cancel every open order, then market-close every open position on
    the named subscriber broker account. Synchronous and idempotent.

    Returns a summary dict so the caller can audit:
        {"cancelled": N, "closed": M, "failures": [{"symbol", "error"}]}
    """
    summary: dict = {"cancelled": 0, "closed": 0, "failures": []}

    acct = db.get(BrokerAccount, broker_account_id)
    if acct is None or acct.user_id != subscriber_user_id:
        log.warning(
            "auto_liquidator: account %s missing or not owned by user %s",
            broker_account_id, subscriber_user_id,
        )
        return summary
    if acct.connection_status != "connected":
        log.warning(
            "auto_liquidator: account %s not connected (status=%s)",
            broker_account_id, acct.connection_status,
        )
        return summary

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
    except Exception as exc:  # noqa: BLE001
        log.exception("auto_liquidator: creds/adapter failed for %s", broker_account_id)
        summary["failures"].append({"symbol": None, "error": str(exc)[:300]})
        return summary

    # ── Step 1: cancel still-working orders ─────────────────────────────
    # If we leave bracket exits / unfilled limits in place, the broker
    # rejects the close (oversold) or fills both the exit AND our close
    # (double position).
    open_orders = db.execute(
        select(Order).where(
            Order.user_id == subscriber_user_id,
            Order.broker_account_id == broker_account_id,
            Order.status.in_(_CANCELLABLE),
            Order.broker_order_id.isnot(None),
        )
    ).scalars().all()
    for o in open_orders:
        try:
            adapter.cancel_order(o.broker_order_id)
            o.status = OrderStatus.CANCELED
            o.closed_at = datetime.now(timezone.utc)
            summary["cancelled"] += 1
        except Exception as exc:  # noqa: BLE001
            # Broker may have already cancelled/filled it — fills_sync will
            # reconcile. Don't fail the liquidation over a stale order.
            log.warning(
                "auto_liquidator: cancel %s failed: %s", o.broker_order_id, exc
            )

    # ── Step 2: list and flatten every open position ────────────────────
    try:
        positions = adapter.get_positions()
    except Exception as exc:  # noqa: BLE001
        log.exception("auto_liquidator: get_positions failed for %s", broker_account_id)
        summary["failures"].append({"symbol": None, "error": f"get_positions: {exc}"[:300]})
        return summary

    for pos in positions:
        if pos.quantity == 0:
            continue
        reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        qty = abs(pos.quantity)
        # Persist a local Order row so the closure surfaces in /trades
        # and the Performance page, exactly like a user-initiated close.
        local = Order(
            user_id=subscriber_user_id,
            broker_account_id=acct.id,
            instrument_type=pos.instrument_type,
            symbol=pos.symbol,
            option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
            option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
            option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
            side=reverse_side,
            order_type=OrderType.MARKET,
            quantity=qty,
            status=OrderStatus.PENDING,
            is_closing=True,
            fanned_out_to_subscribers=False,
        )
        db.add(local)
        db.flush()
        try:
            result = adapter.place_order(
                BrokerOrderRequest(
                    instrument_type=local.instrument_type,
                    symbol=local.symbol,
                    side=local.side,
                    order_type=local.order_type,
                    quantity=local.quantity,
                    option_expiry=local.option_expiry,
                    option_strike=local.option_strike,
                    option_right=local.option_right,
                    client_order_id=str(local.id),
                    is_closing=True,
                )
            )
            local.broker_order_id = result.broker_order_id
            local.status = result.status
            local.submitted_at = result.submitted_at
            summary["closed"] += 1
            audit.record(
                db, actor_user_id=subscriber_user_id,
                action="subscriber.auto_liquidated_position",
                entity_type="order", entity_id=local.id,
                metadata={
                    "symbol": pos.symbol,
                    "side": reverse_side.value,
                    "qty": str(qty),
                    "broker_order_id": result.broker_order_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "auto_liquidator: close %s on %s failed", pos.symbol, broker_account_id
            )
            local.status = OrderStatus.REJECTED
            local.reject_reason = f"auto_liquidate_error: {exc}"[:480]
            local.closed_at = datetime.now(timezone.utc)
            summary["failures"].append({
                "symbol": pos.symbol,
                "error": str(exc)[:300],
            })

    return summary


__all__ = ["liquidate_subscriber_account"]
