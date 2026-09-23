"""Direct-Webull subscriber mirror-order fill reconciler.

The Webull twin of ``alpaca_subscriber_reconciler``. Live trade listeners run
ONLY for TRADER accounts (services.listeners filters role==TRADER), and the
SnapTrade / Alpaca subscriber reconcilers cover those brokers — but a direct
WEBULL SUBSCRIBER account (subscribers may now execute mirrors on direct Webull,
not just via SnapTrade) has NEITHER a real-time listener NOR any other background
fill sync. So a mirror order the copy engine placed on a subscriber's Webull
account, once it fills at the broker, can stay SUBMITTED/working in our DB
indefinitely: order history shows it pending AND close-detection (which reads
filled_quantity) misfires.

Every 30s it finds connected Webull accounts whose owner has no live listener
and that have at least one working order, then refreshes ONLY those orders'
status from the broker via
``fills_sync._refresh_open_orders`` (broker-agnostic — it calls
``adapter.get_order`` per non-terminal order). It deliberately does NOT run any
activities feed, so it never creates synthetic orders; it only ever corrects the
status / filled qty / price of orders we already placed. Worker-only,
best-effort, isolated per account so one bad account can't stall the rest.

Gated behind ``settings.webull_direct_enabled`` — with the flag off (the
default) the loop is never started, exactly like the rest of the direct-Webull
integration.
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

_WORKING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)
_task: "asyncio.Task | None" = None

# Monotonic timestamp of the earliest time each account may be polled again.
# The loop ticks at the FAST interval and uses this to skip accounts that are
# not due — the same shape as pnl_poller's _next_due_at.
_next_due_at: dict[uuid.UUID, float] = {}


def _intervals() -> tuple[float, float, float]:
    from app.config import get_settings  # noqa: PLC0415
    s = get_settings()
    return (
        float(s.webull_subscriber_sync_interval_s),
        float(s.webull_subscriber_sync_idle_interval_s),
        float(s.webull_subscriber_sync_fast_window_s),
    )


def _is_hot(newest: "datetime | None", window_s: float) -> bool:
    """True when this account has order activity recent enough to be worth the
    fast cadence. `newest` is the most recent submit/create time among its
    working orders."""
    if newest is None:
        return False
    if newest.tzinfo is None:                      # SQLite in tests stores naive
        newest = newest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - newest).total_seconds() <= window_s


def start_webull_subscriber_reconciler() -> None:
    """Spawn the reconciler loop. Idempotent. Worker-only — call it where the
    other periodic listeners start (so it never runs on the web tier). The
    caller gates this on ``settings.webull_direct_enabled``."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("webull subscriber reconciler: no running loop; not starting")
        return
    _task = loop.create_task(_run())
    fast, idle, window = _intervals()
    log.info(
        "webull subscriber fill reconciler: started "
        "(fast=%.0fs for %.0fs after activity, idle=%.0fs)",
        fast, window, idle,
    )


async def stop_webull_subscriber_reconciler() -> None:
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
            log.info("webull subscriber fill reconciler: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull subscriber fill reconciler: tick failed")
        await asyncio.sleep(_intervals()[0])


def _reconcile_once() -> None:
    """One sweep: find connected WEBULL accounts that have NO live listener and
    at least one working order, and refresh those orders' status from the broker.
    Only accounts with pending work are polled, so API use tracks real work.

    Scoping is by the ACCOUNT OWNER'S ROLE, not by order shape. It used to
    require ``parent_order_id IS NOT NULL`` — "this account holds copy mirrors,
    therefore it's a subscriber's" — which doubled as the has-pending-work test
    and silently excluded every order the copy engine didn't create:

      * bracket-emulator TP/SL exits (they carry ``bracket_parent_id``, not
        ``parent_order_id``),
      * auto-liquidator and EOD 0DTE closes,
      * Sell-All / manual closes.

    An account whose only working orders were those never got picked up at all,
    so their fills never synced: the rows sat SUBMITTED forever, close-detection
    (which reads ``filled_quantity``) mis-fired, and the position looked open
    after it had been flattened. Selecting on the owner's role instead states the
    real invariant — ``services.listeners`` starts live listeners ONLY for
    ``role == TRADER``, so everyone else needs polling — and it keeps the
    property that mattered: a trader's own account is never double-polled here,
    which on Webull also matters for the rate limit (its trade endpoints share a
    ~10 req/30s budget per app_key with the trader's own order poller).
    """
    fast_s, idle_s, window_s = _intervals()

    with SessionLocal() as db:
        # Accounts with pending work, plus the most recent activity on each —
        # that timestamp is what decides the cadence below.
        newest_by_acct = (
            select(
                Order.broker_account_id.label("acct_id"),
                func.max(
                    func.coalesce(Order.submitted_at, Order.created_at)
                ).label("newest"),
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
                BrokerAccount.broker == BrokerName.WEBULL,
                BrokerAccount.connection_status == "connected",
                # Complement of the listener rule in services.listeners:
                # traders stream their own fills, everyone else is polled.
                User.role != UserRole.TRADER,
            )
        ).all()

    # Adaptive cadence. A mirror is forced to market or a marketable limit, so it
    # fills within seconds of placement — that is the window worth spending
    # Webull's tight budget on (~10 requests / 30s per app_key, shared with the
    # order calls themselves). An order still working after that is a quiet
    # resting limit, where 30s of lag costs nothing and the slots are better left
    # for the copy engine.
    now = time.monotonic()
    acct_ids: list[uuid.UUID] = []
    for acct_id, newest in candidates:
        hot = _is_hot(newest, window_s)
        due = _next_due_at.get(acct_id, 0.0)
        # A freshly-hot account must not wait out a timer set during an idle poll.
        # Without this, a mirror placed 1s after an idle poll inherits that poll's
        # 30s next-due and its fill isn't seen for up to idle_s — the 14-49s lag
        # observed on prod. Bring the next poll forward so a just-placed mirror is
        # caught on the next fast tick. No extra calls: a hot account was due for a
        # fast poll anyway.
        if hot and due > now + fast_s:
            due = now
        if now < due:
            continue
        acct_ids.append(acct_id)
        _next_due_at[acct_id] = now + (fast_s if hot else idle_s)
    # Drop accounts that no longer have working orders so the map can't grow.
    live = {a for a, _ in candidates}
    for gone in [a for a in _next_due_at if a not in live]:
        _next_due_at.pop(gone, None)

    for acct_id in acct_ids:
        _reconcile_account(acct_id)


def _reconcile_account(acct_id: uuid.UUID) -> None:
    """Refresh one account's open orders from the broker. The single apply path
    used by BOTH the poll loop and the gRPC stream trigger (below), so a
    stream-driven refresh and a polled one behave identically. Idempotent."""
    from app.services.fills_sync import _refresh_open_orders  # noqa: PLC0415
    try:
        with SessionLocal() as db:
            acct = db.get(BrokerAccount, acct_id)
            if acct is None or acct.connection_status != "connected":
                return
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            _refresh_open_orders(db, acct, adapter)
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("webull subscriber reconcile: account %s failed", acct_id)


# ── Subscriber gRPC event stream (optional, sub-second fills) ────────────────
# A per-subscriber-account gRPC stream that TRIGGERS _reconcile_account on each
# order event, so a mirror fill is seen in ~0.2s instead of on the next poll.
# The poll loop above stays as the backstop; both call the same idempotent
# _reconcile_account, so a stream-driven refresh and a polled one never conflict.
# The stream is a low-latency TRIGGER only — it never parses fills itself, so the
# apply logic stays in one tested place. Gated by webull_subscriber_stream_enabled
# (default OFF).

_stream_tasks: dict[uuid.UUID, "asyncio.Task"] = {}       # acct_id -> stream task
_stream_supervisor: "asyncio.Task | None" = None
# One order emits several events (submit → fill); don't reconcile more than once
# per this gap — a single refresh already reflects the latest state, and Webull's
# ~10 req/30s budget is shared with the poll and the copy engine.
_stream_last_reconcile: dict[uuid.UUID, float] = {}
_STREAM_DEBOUNCE_S = 1.5


def _stream_enabled() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    s = get_settings()
    return bool(s.webull_direct_enabled and s.webull_subscriber_stream_enabled)


def _on_subscriber_event(acct_id: uuid.UUID) -> None:
    """gRPC callback (runs on the stream thread). Debounced trigger of the same
    reconcile the poll loop runs. _refresh_open_orders self-limits — once the
    order is terminal there's nothing left to poll, so calls fall to zero."""
    now = time.monotonic()
    if now - _stream_last_reconcile.get(acct_id, 0.0) < _STREAM_DEBOUNCE_S:
        return
    _stream_last_reconcile[acct_id] = now
    _reconcile_account(acct_id)


async def _run_subscriber_stream(acct_id: uuid.UUID) -> None:
    """Subscribe to one subscriber account's Webull gRPC event stream; on every
    event trigger an immediate fill reconcile. Reconnect with backoff — same
    shape as webull_listener._run_listener."""
    from app.services.webull_listener import (  # noqa: PLC0415
        _BACKOFF_INITIAL, _BACKOFF_MAX, _all_account_ids, _build_stoppable_client,
        _load_creds,
    )
    backoff = _BACKOFF_INITIAL
    client = None
    while True:
        try:
            creds = _load_creds(acct_id)
            if creds is None or not creds.get("app_key"):
                await asyncio.sleep(30)
                continue
            client = await asyncio.to_thread(_build_stoppable_client, creds)
            client.on_events_message = (
                lambda et, st, payload, raw, _a=acct_id: _on_subscriber_event(_a)
            )
            client.on_log = lambda level, msg, _a=acct_id: log.log(
                level, "webull-sub-stream[%s] SDK: %s", _a, msg,
            )
            account_ids = await asyncio.to_thread(_all_account_ids, creds)
            log.info("webull-sub-stream[%s] subscribing: %s", acct_id, account_ids)
            backoff = _BACKOFF_INITIAL
            # Blocks until the stream ends (stopped, or a non-retryable error).
            await asyncio.to_thread(client.do_subscribe, account_ids)
        except asyncio.CancelledError:
            if client is not None:
                try:
                    client.request_stop()
                except Exception:  # noqa: BLE001
                    pass
            log.info("webull-sub-stream[%s] cancelled", acct_id)
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull-sub-stream[%s] error", acct_id)
        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


def _desired_stream_accounts() -> set[uuid.UUID]:
    """Connected direct-Webull SUBSCRIBER accounts — the same population the poll
    covers (owner is not a TRADER; traders stream their own fills already)."""
    with SessionLocal() as db:
        rows = db.execute(
            select(BrokerAccount.id)
            .join(User, User.id == BrokerAccount.user_id)
            .where(
                BrokerAccount.broker == BrokerName.WEBULL,
                BrokerAccount.connection_status == "connected",
                User.role != UserRole.TRADER,
            )
        ).all()
    return {r[0] for r in rows}


async def _supervise_streams() -> None:
    """Keep a stream task running for each eligible subscriber account: start
    missing, stop orphaned. Re-checks the flag each pass, so toggling the flag
    starts/stops streams without a restart. Cheap DB read every 15s, off-loop."""
    while True:
        try:
            desired = (
                await asyncio.to_thread(_desired_stream_accounts)
                if _stream_enabled() else set()
            )
            loop = asyncio.get_running_loop()
            for acct_id in desired:
                t = _stream_tasks.get(acct_id)
                if t is None or t.done():
                    _stream_tasks[acct_id] = loop.create_task(_run_subscriber_stream(acct_id))
            for acct_id in [a for a in _stream_tasks if a not in desired]:
                t = _stream_tasks.pop(acct_id, None)
                if t is not None and not t.done():
                    t.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull sub-stream supervisor pass failed")
        await asyncio.sleep(15)


def start_webull_subscriber_streams() -> None:
    """Spawn the subscriber-stream supervisor. Idempotent, worker-only. Safe to
    call unconditionally — the supervisor re-checks the flag and runs no streams
    while it's off."""
    global _stream_supervisor
    if _stream_supervisor is not None and not _stream_supervisor.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("webull subscriber streams: no running loop; not starting")
        return
    _stream_supervisor = loop.create_task(_supervise_streams())
    log.info("webull subscriber stream supervisor: started (enabled=%s)", _stream_enabled())


async def stop_webull_subscriber_streams() -> None:
    global _stream_supervisor
    if _stream_supervisor is not None and not _stream_supervisor.done():
        _stream_supervisor.cancel()
        try:
            await _stream_supervisor
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _stream_supervisor = None
    for acct_id in list(_stream_tasks):
        t = _stream_tasks.pop(acct_id, None)
        if t is not None and not t.done():
            t.cancel()
