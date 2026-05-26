"""Webull order-update listener — polling-based.

Why polling, not socket
-----------------------
The unofficial Webull SDK does not expose a stable order-update channel.
Webull's own mobile app uses MQTT internally, but the topic schema and
auth payload are undocumented and have changed multiple times. Building
on that would be high-maintenance for ~no latency win vs. a fast poll.

We poll ``get_current_orders()`` every ``POLL_INTERVAL_S`` (2s by default)
and diff against what we saw last iteration. New orders → fanout.
Existing orders with changed status → status update + cancel cascade
when a trader-side cancel is observed. This gets us end-to-end latency
of roughly 2–4 seconds, comparable to Alpaca's WebSocket once you
account for normal market jitter.

Mirrors the public surface of ``trade_listener.py`` so the same shared
``listener_state`` powers the SSE pill regardless of broker.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers.webull import WebullAdapter, parse_webull_order_symbol
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.models.user import User, UserRole
from app.services import audit, copy_engine, events, listener_state
from app.services.crypto import decrypt_json, encrypt_json

log = logging.getLogger(__name__)


# Poll cadence. Webull internal rate limits accept ~1 req/sec comfortably
# per endpoint; 2s gives headroom while still feeling realtime to users.
POLL_INTERVAL_S = 2.0
# Refresh the session token this many seconds before its declared expiry.
# Webull tokens default to ~24h life; 5 min of slack avoids racing the
# server clock.
REFRESH_SKEW_S = 300


# Active asyncio tasks keyed by trader user_id. Cancelled on stop.
_tasks: dict[uuid.UUID, asyncio.Task] = {}

# Per-trader memory of last-seen order status, used to detect transitions
# without re-reading from the DB on every iteration. Keyed by trader_id;
# inner dict keyed by broker_order_id → status string.
_last_seen: dict[uuid.UUID, dict[str, str]] = {}

# Captured at app startup so sync handler threads can schedule tasks on
# the right loop. See trade_listener.bind_loop for the same pattern.
_main_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


# Re-exports — same wrapper shape as trade_listener.py so call sites are
# symmetric.
get_status = listener_state.get_status
_set_state = listener_state.set_state


# ── Lifecycle ───────────────────────────────────────────────────────────────


async def start_all_listeners() -> None:
    """On app startup, walk every active TRADER with a connected Webull
    account and spawn a poll task. Idempotent."""
    with SessionLocal() as db:
        traders = db.execute(
            select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))
        ).scalars().all()
        for trader in traders:
            for acct in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader.id,
                    BrokerAccount.broker == BrokerName.WEBULL,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars():
                start_listener(trader.id, acct.id)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Spawn (or replace) the Webull poll task for one (trader, account)
    pair. Safe to call from sync handler threads via the captured main loop."""
    existing = _tasks.get(trader_user_id)
    if existing and not existing.done():
        log.info("webull-listener[%s] restart requested", trader_user_id)
        stop_listener(trader_user_id)

    try:
        loop = asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        loop = _main_loop
        on_loop = False

    if loop is None:
        log.warning(
            "webull-listener[%s] no main loop bound; start_listener is a no-op",
            trader_user_id,
        )
        return

    if on_loop:
        task = loop.create_task(_run_listener(trader_user_id, broker_account_id))
        _tasks[trader_user_id] = task
        _set_state(trader_user_id, "connecting")
    else:
        def _schedule() -> None:
            task = loop.create_task(_run_listener(trader_user_id, broker_account_id))
            _tasks[trader_user_id] = task
            _set_state(trader_user_id, "connecting")

        loop.call_soon_threadsafe(_schedule)


def stop_listener(trader_user_id: uuid.UUID) -> None:
    """Cancel the poll task and emit a final 'disconnected' state."""
    task = _tasks.pop(trader_user_id, None)
    if task and not task.done():
        task.cancel()
    _last_seen.pop(trader_user_id, None)
    _set_state(trader_user_id, "disconnected")


async def stop_all_listeners() -> None:
    for tid in list(_tasks.keys()):
        stop_listener(tid)


# ── Poll task ───────────────────────────────────────────────────────────────


_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


async def _run_listener(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> None:
    """Main poll loop. On every iteration:

      1. Refresh the session token if it's near expiry.
      2. Fetch current + recent history orders.
      3. Diff against last-seen and emit ``order.placed`` /
         ``order.copy_*`` events via the copy engine.
      4. Sleep ``POLL_INTERVAL_S``.

    Outer loop reconnects with exponential backoff on transient errors.
    """
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            # Load + verify creds. This re-reads on every reconnect so
            # MFA-re-completed sessions pick up without restarting the task.
            creds = _load_creds(trader_user_id, broker_account_id)
            if creds is None:
                _set_state(
                    trader_user_id,
                    "credentials_invalid",
                    error="broker disconnected or credentials missing",
                )
                await asyncio.sleep(30)
                backoff = _BACKOFF_INITIAL
                continue

            adapter = WebullAdapter(creds)
            # First connect: refresh the session so we know we're alive.
            try:
                adapter._refresh_if_needed()  # noqa: SLF001 — internal but stable
                _persist_creds_if_changed(broker_account_id, adapter.credentials, creds)
            except Exception as exc:  # noqa: BLE001
                # If we can't refresh, the user needs to reconnect. Surface
                # this clearly rather than retrying forever.
                _set_state(trader_user_id, "credentials_invalid", error=str(exc)[:300])
                await asyncio.sleep(60)
                continue

            _set_state(trader_user_id, "connected")
            backoff = _BACKOFF_INITIAL

            # Inner poll loop. Breaks out on adapter error so we re-enter
            # the outer retry/backoff.
            while True:
                try:
                    await asyncio.to_thread(
                        _poll_once, trader_user_id, broker_account_id, adapter
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "webull-listener[%s] poll iteration failed", trader_user_id
                    )
                    _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])
                    break  # outer loop will reconnect
                await asyncio.sleep(POLL_INTERVAL_S)

        except asyncio.CancelledError:
            log.info("webull-listener[%s] cancelled", trader_user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("webull-listener[%s] error: %s", trader_user_id, exc)
            _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])

        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


# ── Credential helpers ──────────────────────────────────────────────────────


def _load_creds(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return decrypted Webull credentials, or None if the account is gone /
    disconnected / wrong broker."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if (
            acct is None
            or acct.user_id != trader_user_id
            or acct.broker != BrokerName.WEBULL
            or acct.connection_status != "connected"
        ):
            return None
        try:
            return decrypt_json(acct.encrypted_credentials)
        except Exception:  # noqa: BLE001
            log.exception(
                "webull-listener[%s] failed to decrypt credentials", trader_user_id
            )
            return None


def _persist_creds_if_changed(
    broker_account_id: uuid.UUID,
    new_creds: dict[str, Any],
    old_creds: dict[str, Any],
) -> None:
    """Webull session tokens rotate on refresh. Persist the new session
    blob so a restart doesn't force a full re-login. Only touches the DB
    when the session sub-dict actually changed."""
    if new_creds.get("session") == old_creds.get("session"):
        return
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if acct is None:
            return
        acct.encrypted_credentials = encrypt_json(new_creds)
        db.commit()


# ── Poll iteration ──────────────────────────────────────────────────────────


_BUY = OrderSide.BUY
_SELL = OrderSide.SELL


def _poll_once(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    adapter: WebullAdapter,
) -> None:
    """Sync — runs in a thread. Pulls recent orders from Webull, diffs
    against last-seen, and routes new/changed orders through the same
    persist+fanout pipeline as the Alpaca listener."""
    orders = adapter.list_recent_activities()
    if not orders:
        listener_state.bump_last_event(trader_user_id)
        return

    seen = _last_seen.setdefault(trader_user_id, {})
    listener_state.bump_last_event(trader_user_id)

    for o in orders:
        broker_order_id = str(_attr(o, "orderId", "id", default=""))
        if not broker_order_id:
            continue
        status_str = str(_attr(o, "status", "statusStr", default=""))
        prev_status = seen.get(broker_order_id)
        if prev_status == status_str:
            continue  # no change since last iteration
        seen[broker_order_id] = status_str

        _persist_and_fanout(
            trader_user_id, broker_account_id, broker_order_id, status_str, o
        )


def _persist_and_fanout(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    broker_order_id: str,
    status_str: str,
    order_obj: Any,
) -> None:
    """Insert a new Order or update an existing one, then mirror to
    subscribers via the copy engine when appropriate."""
    from app.brokers.webull import _STATUS_IN as WEBULL_STATUS_IN

    status_enum = WEBULL_STATUS_IN.get(status_str, OrderStatus.SUBMITTED)

    with SessionLocal() as db:
        existing = db.execute(
            select(Order).where(Order.broker_order_id == broker_order_id)
        ).scalar_one_or_none()

        if existing is not None:
            # Status / fills update path.
            if existing.status != status_enum:
                existing.status = status_enum
            fq = _attr(order_obj, "filledQuantity", "filledQty")
            if fq is not None:
                try:
                    existing.filled_quantity = Decimal(str(fq))
                except Exception:  # noqa: BLE001
                    pass
            fap = _attr(order_obj, "avgFilledPrice", "filledAvgPrice")
            if fap is not None:
                try:
                    existing.filled_avg_price = Decimal(str(fap))
                except Exception:  # noqa: BLE001
                    pass
            if status_enum in (
                OrderStatus.FILLED, OrderStatus.CANCELED,
                OrderStatus.REJECTED, OrderStatus.EXPIRED,
            ) and existing.closed_at is None:
                existing.closed_at = datetime.now(timezone.utc)
            if existing.socket_received_at is None:
                existing.socket_received_at = datetime.now(timezone.utc)
            existing.redis_published_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing)
            events.publish(
                trader_user_id,
                copy_engine._order_event("order.placed", existing),  # noqa: SLF001
            )
            # Cancel cascade — same as Alpaca listener.
            if (
                status_str.lower() in ("cancelled", "canceled", "expired", "rejected", "failed")
                and existing.parent_order_id is None
                and existing.fanned_out_to_subscribers
            ):
                _cascade_cancel_to_mirrors(existing.id)
            return

        # Brand-new order — only act on working/terminal-success states.
        # PENDING-style noise (e.g. queued before Webull accepted it) is
        # skipped so we don't fanout a placeholder that immediately fails.
        if status_enum not in (
            OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        ):
            return

        order = _insert_order_from_webull(
            db, trader_user_id, broker_account_id, broker_order_id, order_obj, status_enum
        )

        # Stamp lifecycle timestamps so the Performance page can compute
        # poll-lag the same way it does socket-lag for Alpaca. Note
        # `socket_received_at` is reused here even though there's no
        # socket — naming kept for cross-broker consistency.
        order.trader_submitted_at = _as_dt(_attr(order_obj, "placedTime", "createTime"))
        order.socket_received_at = datetime.now(timezone.utc)

        audit.record(
            db,
            actor_user_id=trader_user_id,
            action="listener.order_observed",
            entity_type="order",
            entity_id=order.id,
            metadata={
                "broker": "webull",
                "broker_order_id": broker_order_id,
                "status": status_str,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": str(order.quantity),
            },
        )
        order.redis_published_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(order)

        events.publish(
            trader_user_id,
            copy_engine._order_event("order.placed", order),  # noqa: SLF001
        )

        trader = db.get(User, trader_user_id)
        if trader is not None:
            copy_engine.fanout(db, order, trader)
            order.fanned_out_to_subscribers = True
            db.commit()


def _insert_order_from_webull(
    db: Any,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    broker_order_id: str,
    order_obj: Any,
    status_enum: OrderStatus,
) -> Order:
    """Translate a Webull order dict into our Order schema and INSERT.

    Detects stock vs option via ``parse_webull_order_symbol`` so option
    orders placed externally on Webull are routed through Option
    Haven's option views correctly, with expiry/strike/right populated
    instead of being mis-tagged as stocks."""
    parsed = parse_webull_order_symbol(order_obj)

    # Webull's option actions use the same BUY/SELL pair as stocks
    # (no _TO_OPEN / _TO_CLOSE suffix); positionEffect or closePosition
    # is sometimes carried separately. We default is_closing=False since
    # Webull doesn't always surface a reliable signal.
    side_raw = str(_attr(order_obj, "action", default="")).upper()
    side = _BUY if side_raw == "BUY" else _SELL

    type_raw = str(_attr(order_obj, "orderType", default="")).upper()
    order_type = {
        "MKT":     OrderType.MARKET,
        "LMT":     OrderType.LIMIT,
        "STP":     OrderType.STOP,
        "STP LMT": OrderType.STOP_LIMIT,
    }.get(type_raw, OrderType.MARKET)

    qty = _to_dec(_attr(order_obj, "totalQuantity", "quantity")) or Decimal(0)
    limit_price = _to_dec(_attr(order_obj, "lmtPrice", "limitPrice"))
    stop_price = _to_dec(_attr(order_obj, "auxPrice", "stopPrice"))
    filled_q = _to_dec(_attr(order_obj, "filledQuantity", "filledQty")) or Decimal(0)
    filled_avg = _to_dec(_attr(order_obj, "avgFilledPrice", "filledAvgPrice"))
    submitted_at = (
        _as_dt(_attr(order_obj, "placedTime", "createTime"))
        or datetime.now(timezone.utc)
    )

    order = Order(
        user_id=trader_user_id,
        broker_account_id=broker_account_id,
        instrument_type=parsed["instrument_type"],
        symbol=parsed["symbol"],
        option_expiry=parsed["option_expiry"],
        option_strike=parsed["option_strike"],
        option_right=parsed["option_right"],
        side=side,
        order_type=order_type,
        quantity=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        status=status_enum,
        broker_order_id=broker_order_id,
        filled_quantity=filled_q,
        filled_avg_price=filled_avg,
        submitted_at=submitted_at,
        closed_at=(
            datetime.now(timezone.utc) if status_enum in (
                OrderStatus.FILLED, OrderStatus.CANCELED,
                OrderStatus.REJECTED, OrderStatus.EXPIRED,
            ) else None
        ),
        fanned_out_to_subscribers=False,
    )
    db.add(order)
    db.flush()
    return order


def _cascade_cancel_to_mirrors(parent_order_id: uuid.UUID) -> None:
    from app.api.trades import _run_cancel_fanout_in_background
    try:
        _run_cancel_fanout_in_background(parent_order_id)
    except Exception:  # noqa: BLE001
        log.exception("webull-listener cancel-cascade failed for %s", parent_order_id)


# ── Small helpers (mirroring trade_listener's local utils) ──────────────────


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _to_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        try:
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
