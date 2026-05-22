"""Startup recovery for orphaned PENDING child orders.

The no-worker fanout (FastAPI BackgroundTasks + asyncio.gather) is fast and
simple but has one failure mode: if the process is killed mid-fanout, child
Orders that made it through Phase 1 (DB row inserted, status=PENDING) but
never reached the broker stay PENDING forever.

This sweep runs on FastAPI startup. Strategy:
  - find child orders (parent_order_id IS NOT NULL) older than RECOVERY_AGE
    that are still PENDING
  - re-trigger fanout for each parent by rebuilding the broker request from
    the child row and placing it directly (skip Phase 1, we already have the
    child row to update)

Conservatively bounded — we only sweep on startup, not on a timer, to keep
behavior predictable. If you need periodic recovery, schedule this from a
cron.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderStatus
from app.services import audit, cache, copy_engine, events

log = logging.getLogger(__name__)

RECOVERY_AGE = timedelta(seconds=60)
MAX_RECOVERY_BATCH = 500


async def sweep_orphaned_pending() -> int:
    """Replay broker calls for child orders that were stranded mid-fanout.
    Returns the number of orders processed."""
    cutoff = datetime.now(timezone.utc) - RECOVERY_AGE
    with SessionLocal() as db:
        stranded = list(
            db.execute(
                select(Order)
                .where(
                    Order.status == OrderStatus.PENDING,
                    Order.parent_order_id.is_not(None),
                    Order.created_at < cutoff,
                )
                .limit(MAX_RECOVERY_BATCH)
            ).scalars()
        )
        if not stranded:
            return 0
        log.info("recovery: replaying %d stranded child orders", len(stranded))

        async def _replay(child: Order) -> None:
            acct = db.get(BrokerAccount, child.broker_account_id)
            if acct is None:
                child.status = OrderStatus.REJECTED
                child.reject_reason = "broker_account_missing"
                child.closed_at = datetime.now(timezone.utc)
                return
            try:
                creds = cache.decrypt_creds_cached(acct.id, acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
                req = BrokerOrderRequest(
                    instrument_type=child.instrument_type,
                    symbol=child.symbol,
                    side=child.side,
                    order_type=child.order_type,
                    quantity=child.quantity,
                    limit_price=child.limit_price,
                    stop_price=child.stop_price,
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    client_order_id=str(child.id),
                )
                resp = await asyncio.to_thread(adapter.place_order, req)
                child.status = resp.status
                child.broker_order_id = resp.broker_order_id
                child.submitted_at = resp.submitted_at
                child.filled_quantity = resp.filled_quantity
                child.filled_avg_price = resp.filled_avg_price
                audit.record(
                    db,
                    actor_user_id=child.user_id,
                    action="copy.recovered",
                    entity_type="order",
                    entity_id=child.id,
                    metadata={"broker_order_id": resp.broker_order_id},
                )
                events.publish(child.user_id, copy_engine._order_event("order.copy_submitted", child))
            except Exception as exc:  # noqa: BLE001
                child.status = OrderStatus.REJECTED
                child.reject_reason = f"recovery_failed: {str(exc)[:400]}"
                child.closed_at = datetime.now(timezone.utc)
                audit.record(
                    db,
                    actor_user_id=child.user_id,
                    action="copy.recovery_failed",
                    entity_type="order",
                    entity_id=child.id,
                    metadata={"error": str(exc)[:400]},
                )

        await asyncio.gather(*(_replay(c) for c in stranded))
        db.commit()
        return len(stranded)
