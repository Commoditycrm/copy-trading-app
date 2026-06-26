"""Broker-agnostic listener dispatcher.

Callers (FastAPI lifespan, /api/brokers connect/disconnect, etc.) talk to
this module instead of importing ``trade_listener`` / ``ibkr_listener`` /
``snaptrade_listener`` directly. Routing is by ``BrokerName``.

Direct Webull integration has been removed — users connect Webull through
SnapTrade (which lands as ``BrokerName.SNAPTRADE`` rows handled by
``snaptrade_listener``).

Status reads are unified: ``listener_state.get_status`` returns the live
state regardless of which broker actually drives it. All backends share
the same status dict.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.services import (
    ibkr_listener,
    listener_state,
    snaptrade_listener,
    trade_listener,
)

log = logging.getLogger(__name__)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Forward to every backend. Cheap — just stores a reference."""
    trade_listener.bind_loop(loop)
    snaptrade_listener.bind_loop(loop)
    ibkr_listener.bind_loop(loop)


async def start_all_listeners() -> None:
    """Spawn listeners for every connected broker account on app startup."""
    await trade_listener.start_all_listeners()
    await snaptrade_listener.start_all_listeners()
    await ibkr_listener.start_all_listeners()


async def stop_all_listeners() -> None:
    """Symmetric shutdown — every backend drains its tasks."""
    await trade_listener.stop_all_listeners()
    await snaptrade_listener.stop_all_listeners()
    await ibkr_listener.stop_all_listeners()


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Ensure a listener is running for this (trader, account).

    Listeners must live in EXACTLY ONE process. In the web/worker split the
    web tier (run_background_workers=false) must NOT start a listener itself —
    doing so duplicates the worker's listener and double-mirrors every trade.
    So the web tier hands the request to the worker over Redis; the worker (and
    any single-process deployment) starts it locally."""
    from app.config import get_settings

    if get_settings().run_background_workers:
        _start_listener_local(trader_user_id, broker_account_id)
    else:
        from app.services import listener_control
        listener_control.request_start(trader_user_id, broker_account_id)


def _start_listener_local(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Actually spawn the listener in THIS process. Route to the right backend
    based on the account's broker. Looks up the BrokerAccount row to avoid
    making callers pass the broker name."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
    if acct is None:
        log.warning(
            "listeners.start_listener: account %s not found", broker_account_id
        )
        return
    if acct.broker == BrokerName.ALPACA:
        trade_listener.start_listener(trader_user_id, broker_account_id)
    elif acct.broker == BrokerName.SNAPTRADE:
        snaptrade_listener.start_listener(trader_user_id, broker_account_id)
    elif acct.broker == BrokerName.IBKR:
        ibkr_listener.start_listener(trader_user_id, broker_account_id)
    else:
        # Includes BrokerName.WEBULL (dormant — historical rows only) and
        # BrokerName.FAKE (no live listener needed).
        log.info(
            "listeners.start_listener: no listener for broker %s",
            acct.broker.value,
        )


def stop_listener(trader_user_id: uuid.UUID) -> None:
    """Stop this trader's listener. Like start_listener, the web tier doesn't
    own the listener task, so it routes the stop to the worker; the worker /
    single-process deployment stops it locally."""
    from app.config import get_settings

    if get_settings().run_background_workers:
        _stop_listener_local(trader_user_id)
    else:
        from app.services import listener_control
        listener_control.request_stop(trader_user_id)


def _stop_listener_local(trader_user_id: uuid.UUID) -> None:
    """Stop whichever backend is currently servicing this trader, in THIS
    process. One-broker-per-user means at most one will have a task — but we
    call every backend so transitions (Alpaca → SnapTrade → IBKR or any
    other permutation) are always clean.

    All calls publish a final ``disconnected`` state via
    ``listener_state.set_state``; subsequent calls are no-ops for
    status purposes (already disconnected) but safely remove stragglers."""
    trade_listener.stop_listener(trader_user_id)
    snaptrade_listener.stop_listener(trader_user_id)
    ibkr_listener.stop_listener(trader_user_id)
    # Drop the entry entirely so the SSE pill doesn't keep showing a
    # 'disconnected' state for a broker the user no longer has.
    listener_state.clear(trader_user_id)


def _running_listener_trader_ids() -> set[uuid.UUID]:
    """Trader ids with a live (not-done) listener task in THIS process, across
    all backends. Reads each backend's task registry defensively so a backend
    without one just contributes nothing."""
    out: set[uuid.UUID] = set()
    for mod in (trade_listener, snaptrade_listener, ibkr_listener):
        tasks = getattr(mod, "_tasks", {})
        for tid, task in list(tasks.items()):
            try:
                if task is not None and not task.done():
                    out.add(tid)
            except Exception:  # noqa: BLE001 — never let one bad task block the sweep
                out.add(tid)
    return out


def reconcile_once() -> None:
    """Desired-state sync: make the listeners running in THIS process match the
    connected trader broker accounts in the DB. Starts any missing, stops any
    orphaned. Idempotent — safe to run on a timer.

    Why this exists: the web tier hands listener start/stop to the worker over
    Redis pub/sub (``listener_control``), which is fire-and-forget — a message
    missed during a worker restart, a Redis blip, or a race leaves a connected
    trader with NO listener (their status pill reads offline) until the next
    process restart. This periodic reconcile is the safety net that heals that;
    pub/sub stays as the low-latency fast path.

    MUST run OFF the event loop — it issues a synchronous psycopg query, which
    would freeze the whole worker loop (and with it every listener, the SSE
    feed, and health) if run inline. ``run_reconciler`` calls it via
    ``asyncio.to_thread``. The start/stop helpers it calls are already safe to
    invoke off-loop (they marshal task creation/cancellation onto the loop the
    same way the web tier's sync request handlers do)."""
    from sqlalchemy import select  # noqa: PLC0415
    from app.models.user import User, UserRole  # noqa: PLC0415

    desired: dict[uuid.UUID, uuid.UUID] = {}  # trader_id -> broker_account_id
    with SessionLocal() as db:
        rows = db.execute(
            select(BrokerAccount.user_id, BrokerAccount.id)
            .join(User, User.id == BrokerAccount.user_id)
            .where(
                User.role == UserRole.TRADER,
                User.is_active.is_(True),
                BrokerAccount.connection_status == "connected",
                BrokerAccount.broker.in_(
                    [BrokerName.ALPACA, BrokerName.SNAPTRADE, BrokerName.IBKR]
                ),
            )
        ).all()
    for user_id, acct_id in rows:
        # One-broker-per-user; if duplicates ever exist, last wins (arbitrary
        # but stable enough — the listener restarts cleanly either way).
        desired[user_id] = acct_id

    running = _running_listener_trader_ids()
    desired_ids = set(desired.keys())

    for trader_id, acct_id in desired.items():
        if trader_id not in running:
            log.info("reconcile: starting missing listener for trader %s", trader_id)
            _start_listener_local(trader_id, acct_id)

    for trader_id in running - desired_ids:
        log.info("reconcile: stopping orphaned listener for trader %s", trader_id)
        _stop_listener_local(trader_id)

    # Clear stale status keys: a trader with neither a connected broker (not
    # desired) nor a live task (not running) but still a lingering
    # listener:state key — e.g. a disconnect whose stop message was lost, or a
    # listener that died across a restart. Without this their pill (and any
    # subscriber following them) shows "online" forever.
    for tid_str in list(listener_state.get_all_statuses().keys()):
        try:
            tid = uuid.UUID(tid_str)
        except ValueError:
            continue
        if tid not in desired_ids and tid not in running:
            log.info("reconcile: clearing stale listener state for %s", tid)
            listener_state.clear(tid)


async def run_reconciler(interval_s: float = 10.0) -> None:
    """Worker-only loop that runs :func:`reconcile_once` forever, OFF the event
    loop (its DB query must not block the loop). A failed pass is logged and
    retried next tick — one bad sweep never kills the loop."""
    while True:
        try:
            await asyncio.to_thread(reconcile_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("listener reconcile pass failed")
        await asyncio.sleep(interval_s)


# Backwards-compat re-export — some call sites still import get_status from
# trade_listener. listener_state is the source of truth now.
get_status = listener_state.get_status
