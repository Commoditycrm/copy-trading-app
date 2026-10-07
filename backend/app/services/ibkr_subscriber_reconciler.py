"""IBKR subscriber mirror-order status reconciler.

The IBKR twin of ``webull_subscriber_reconciler``. Live listeners run ONLY for
TRADER accounts (services.listeners filters role==TRADER), so a mirror the copy
engine places on a SUBSCRIBER's IBKR account has nothing refreshing its status:
once it fills — or once IBKR cancels it, which it does on the spot for a MARKET
order that arrives outside regular hours — the row stays SUBMITTED in our DB
forever. Order history shows it working, close-detection (which reads
filled_quantity) misfires, and a cancel from the UI hits "OrderID doesn't exist"
(paper, 2026-10-07).

Every few seconds it finds connected IBKR accounts whose owner is not a trader
and that have at least one working order, and refreshes ONLY those orders via
``fills_sync._refresh_open_orders`` (broker-agnostic: ``adapter.get_order`` per
non-terminal order, which on IBKR is one ``/iserver/account/orders`` read). It
never creates orders. Best-effort, isolated per account.

Cadence: fast while an account has had order activity in the last couple of
minutes (a mirror fills within seconds of placement), idle otherwise. All calls
go through the account's gateway session, which the trader-side poll or the
gateway keepalive already keeps alive.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

FAST_INTERVAL_S = 5.0
IDLE_INTERVAL_S = 30.0
FAST_WINDOW_S = 120.0

_WORKING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)
_task: "asyncio.Task | None" = None
_next_due_at: dict[uuid.UUID, float] = {}


def _is_hot(newest: "datetime | None") -> bool:
    if newest is None:
        return False
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - newest).total_seconds() <= FAST_WINDOW_S


def start_ibkr_subscriber_reconciler() -> None:
    """Spawn the loop. Idempotent. Worker-only — start it where the other
    periodic listeners start."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("ibkr subscriber reconciler: no running loop; not starting")
        return
    _task = loop.create_task(_run())
    log.info(
        "ibkr subscriber order reconciler: started (fast=%.0fs for %.0fs after activity, idle=%.0fs)",
        FAST_INTERVAL_S, FAST_WINDOW_S, IDLE_INTERVAL_S,
    )


async def stop_ibkr_subscriber_reconciler() -> None:
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
            await asyncio.to_thread(reconcile_once)
        except asyncio.CancelledError:
            log.info("ibkr subscriber order reconciler: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("ibkr subscriber order reconciler: tick failed")
        await asyncio.sleep(FAST_INTERVAL_S)


def reconcile_once() -> int:
    """One sweep. Returns the number of accounts refreshed."""
    from app.services.fills_sync import _refresh_open_orders  # noqa: PLC0415

    with SessionLocal() as db:
        newest_by_acct = (
            select(
                Order.broker_account_id.label("acct_id"),
                func.max(func.coalesce(Order.submitted_at, Order.created_at)).label("newest"),
            )
            .where(
                Order.status.in_(_WORKING_STATUSES),
                Order.broker_order_id.is_not(None),
                Order.broker_account_id.is_not(None),
            )
            .group_by(Order.broker_account_id)
            .subquery()
        )
        candidates = db.execute(
            select(BrokerAccount.id, newest_by_acct.c.newest)
            .join(User, User.id == BrokerAccount.user_id)
            .join(newest_by_acct, newest_by_acct.c.acct_id == BrokerAccount.id)
            .where(
                BrokerAccount.broker == BrokerName.IBKR,
                BrokerAccount.connection_status == "connected",
                # Traders have the live ibkr_listener; everyone else is polled.
                User.role != UserRole.TRADER,
            )
        ).all()

    now = time.monotonic()
    acct_ids: list[uuid.UUID] = []
    for acct_id, newest in candidates:
        hot = _is_hot(newest)
        due = _next_due_at.get(acct_id, 0.0)
        if hot and due > now + FAST_INTERVAL_S:
            due = now
        if now < due:
            continue
        acct_ids.append(acct_id)
        _next_due_at[acct_id] = now + (FAST_INTERVAL_S if hot else IDLE_INTERVAL_S)
    live = {a for a, _ in candidates}
    for gone in [a for a in _next_due_at if a not in live]:
        _next_due_at.pop(gone, None)

    done = 0
    for acct_id in acct_ids:
        try:
            with SessionLocal() as db:
                acct = db.get(BrokerAccount, acct_id)
                if acct is None or acct.connection_status != "connected":
                    continue
                adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
                _refresh_open_orders(db, acct, adapter)
                db.commit()
                done += 1
        except Exception:  # noqa: BLE001
            log.exception("ibkr subscriber reconcile: account %s failed", acct_id)
    return done
