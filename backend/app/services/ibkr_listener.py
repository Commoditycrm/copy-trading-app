"""IBKR order-update listener — polling-based.

Why polling: IBKR's OAuth Web API doesn't expose a hosted WebSocket for
order updates; that's a CPAPI (gateway) feature and we explicitly avoid
the gateway to keep the integration SaaS-friendly. So we poll IBKR's
``/iserver/account/orders`` directly every ``POLL_INTERVAL_S``. Because
we're talking to IBKR (not an aggregator that itself polls), end-to-end
mirror latency is typically 2–5s — far better than the SnapTrade path
(5–60s).

Mirrors the public surface of ``snaptrade_listener.py`` so the shared
``listener_state`` + SSE pill work the same regardless of broker. Uses
``copy_engine.fanout_threadsafe`` to dispatch the fanout onto the main
event loop (avoids the throwaway-loop-per-order cross-loop semaphore
bug; see copy_engine.fanout_threadsafe docstring).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers.ibkr import IBKRAdapter
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
from app.services import audit, broker_filters, copy_engine, events, listener_state
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


# 3s is a fair default — fast enough to keep mirror latency in the 2–5s
# range, gentle enough not to blow IBKR's rate budget. Tune via env if
# someone wants tighter latency at the cost of more calls.
POLL_INTERVAL_S = 3.0


_tasks: dict[uuid.UUID, asyncio.Task] = {}
# Per-trader (broker_order_id → last status) — dedup so we only persist a
# status transition once.
_last_seen: dict[uuid.UUID, dict[str, str]] = {}
# Per-trader lock so we can't race ourselves on the SELECT-then-INSERT
# dedup inside _persist_and_fanout.
_poll_locks: dict[uuid.UUID, threading.Lock] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


def _lock_for(trader_user_id: uuid.UUID) -> threading.Lock:
    return _poll_locks.setdefault(trader_user_id, threading.Lock())


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


get_status = listener_state.get_status
_set_state = listener_state.set_state


# ── Lifecycle ───────────────────────────────────────────────────────────────


async def start_all_listeners() -> None:
    """On app startup, spawn a poll task for every active TRADER with a
    connected IBKR account."""
    with SessionLocal() as db:
        traders = db.execute(
            select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))
        ).scalars().all()
        for trader in traders:
            for acct in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader.id,
                    BrokerAccount.broker == BrokerName.IBKR,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars():
                start_listener(trader.id, acct.id)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    existing = _tasks.get(trader_user_id)
    if existing and not existing.done():
        log.info("ibkr-listener[%s] restart requested", trader_user_id)
        stop_listener(trader_user_id)

    try:
        loop = asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        loop = _main_loop
        on_loop = False

    if loop is None:
        log.warning(
            "ibkr-listener[%s] no main loop bound; start_listener is a no-op",
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
    task = _tasks.pop(trader_user_id, None)
    if task and not task.done():
        task.cancel()
    _last_seen.pop(trader_user_id, None)
    _poll_locks.pop(trader_user_id, None)
    _set_state(trader_user_id, "disconnected")


async def stop_all_listeners() -> None:
    for tid in list(_tasks.keys()):
        stop_listener(tid)


def has_running_listener(trader_user_id: uuid.UUID) -> bool:
    """True if this backend has a live (not-done) task for the trader. Lets
    listeners.reconcile() avoid restarting a healthy listener every tick."""
    t = _tasks.get(trader_user_id)
    return t is not None and not t.done()


def running_trader_ids() -> set[uuid.UUID]:
    """Trader ids with a live task here. Snapshots _tasks so a concurrent
    start/stop on the loop can't mutate the dict mid-iteration."""
    return {tid for tid, t in list(_tasks.items()) if not t.done()}


# ── Poll task ───────────────────────────────────────────────────────────────


_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


async def _run_listener(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> None:
    """Outer loop: load creds → verify → inner poll loop → reconnect.
    Same shape as snaptrade_listener._run_listener."""
    backoff = _BACKOFF_INITIAL
    while True:
        try:
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

            adapter = IBKRAdapter(creds)
            # Verify auth before settling into the inner poll loop —
            # surface "creds rotated / token expired" cleanly instead of
            # the whole poll loop spinning on 401s.
            try:
                await asyncio.to_thread(adapter.verify_connection)
            except Exception as exc:  # noqa: BLE001
                listener_state.notify_broker_disconnected(trader_user_id, broker_account_id, str(exc))
                _set_state(trader_user_id, "credentials_invalid", error=str(exc)[:300])
                await asyncio.sleep(60)
                continue

            listener_state.clear_disconnect_debounce(trader_user_id)
            _set_state(trader_user_id, "connected")
            backoff = _BACKOFF_INITIAL

            while True:
                try:
                    await asyncio.to_thread(
                        _poll_once, trader_user_id, broker_account_id, adapter
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "ibkr-listener[%s] poll iteration failed", trader_user_id
                    )
                    _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])
                    break
                await asyncio.sleep(POLL_INTERVAL_S)

        except asyncio.CancelledError:
            log.info("ibkr-listener[%s] cancelled", trader_user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("ibkr-listener[%s] error: %s", trader_user_id, exc)
            _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])

        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


# ── Credential helpers ──────────────────────────────────────────────────────


def _load_creds(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> dict[str, Any] | None:
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if (
            acct is None
            or acct.user_id != trader_user_id
            or acct.broker != BrokerName.IBKR
            or acct.connection_status != "connected"
        ):
            return None
        try:
            return decrypt_json(acct.encrypted_credentials)
        except Exception:  # noqa: BLE001
            log.exception(
                "ibkr-listener[%s] failed to decrypt credentials", trader_user_id
            )
            return None


# ── Poll iteration ──────────────────────────────────────────────────────────


_BUY = OrderSide.BUY
_SELL = OrderSide.SELL


# IBKR → our OrderType.
_TYPE_IN = {
    "MKT":     OrderType.MARKET,
    "LMT":     OrderType.LIMIT,
    "STP":     OrderType.STOP,
    "STP_LMT": OrderType.STOP_LIMIT,
}


def _poll_once(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    adapter: IBKRAdapter,
) -> None:
    """Pull recent orders, diff against last-seen, route changes through
    the persist+fanout pipeline. Sync — runs in a thread.

    Guarded by a per-trader lock so future periodic + ad-hoc polls don't
    race on the SELECT-then-INSERT dedup."""
    with _lock_for(trader_user_id):
        # Gate: master switch off → skip the broker fetch + processing.
        # Re-read every poll so a runtime flip of "Auto Pull Orders" on
        # the Brokers page takes effect immediately.
        with SessionLocal() as db:
            acct = db.get(BrokerAccount, broker_account_id)
            if not broker_filters.auto_pull_enabled(acct):
                listener_state.bump_last_event(trader_user_id)
                return
        orders = adapter.list_recent_activities()
        listener_state.bump_last_event(trader_user_id)
        if not orders:
            return

        seen = _last_seen.setdefault(trader_user_id, {})
        for o in orders:
            broker_order_id = str(_attr(o, "orderId", "order_id", "id") or "")
            if not broker_order_id:
                continue
            status_str = str(_attr(o, "status", "orderStatus") or "").upper().replace(" ", "_")
            prev = seen.get(broker_order_id)
            if prev == status_str:
                continue
            seen[broker_order_id] = status_str

            _persist_and_fanout(
                trader_user_id, broker_account_id, broker_order_id, status_str, o
            )


# IBKR order status → our enum (same mapping as the adapter, kept local
# so the listener doesn't import the adapter's internal dicts).
_STATUS_IN: dict[str, OrderStatus] = {
    "PENDINGSUBMIT":    OrderStatus.PENDING,
    "PENDING_SUBMIT":   OrderStatus.PENDING,
    "PRESUBMITTED":     OrderStatus.SUBMITTED,
    "PRE_SUBMITTED":    OrderStatus.SUBMITTED,
    "SUBMITTED":        OrderStatus.SUBMITTED,
    "ACCEPTED":         OrderStatus.ACCEPTED,
    "FILLED":           OrderStatus.FILLED,
    "PARTIALLYFILLED":  OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED":        OrderStatus.CANCELED,
    "CANCELED":         OrderStatus.CANCELED,
    "REJECTED":         OrderStatus.REJECTED,
    "INACTIVE":         OrderStatus.REJECTED,
    "EXPIRED":          OrderStatus.EXPIRED,
}


def _persist_and_fanout(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    broker_order_id: str,
    status_str: str,
    order_obj: Any,
) -> None:
    status_enum = _STATUS_IN.get(status_str, OrderStatus.SUBMITTED)

    with SessionLocal() as db:
        acct_gate = db.get(BrokerAccount, broker_account_id)
        if not broker_filters.should_persist_order(acct_gate, status_enum):
            return
        existing = db.execute(
            select(Order).where(Order.broker_order_id == broker_order_id)
        ).scalar_one_or_none()

        if existing is not None:
            # Track whether *this* poll observed a status transition — the
            # bracket emulator hooks below only fire on actual transitions
            # so we don't pay for them on every quiescent poll.
            status_changed = existing.status != status_enum
            if status_changed:
                existing.status = status_enum
            fq = _attr(order_obj, "filledQuantity", "cumQty")
            if fq is not None:
                try:
                    existing.filled_quantity = Decimal(str(fq))
                except Exception:  # noqa: BLE001
                    pass
            fap = _attr(order_obj, "avgPrice", "lastPrice")
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
            # Bracket emulator hooks — see snaptrade_listener for the same
            # pattern. Both functions short-circuit on inputs they don't
            # match, so calling them together is safe.
            if status_changed and status_enum == OrderStatus.FILLED:
                try:
                    from app.services.bracket_emulator import (  # noqa: PLC0415
                        cancel_sibling_on_fill,
                        emulate_bracket_exits,
                    )
                    emulate_bracket_exits(db, existing)
                    cancel_sibling_on_fill(db, existing)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "ibkr_listener: bracket emulator failed for order %s",
                        existing.id,
                    )
            db.commit()
            db.refresh(existing)
            events.publish(
                trader_user_id,
                copy_engine._order_event("order.placed", existing),  # noqa: SLF001
            )
            if (
                status_str in ("CANCELLED", "CANCELED", "EXPIRED", "REJECTED")
                and existing.parent_order_id is None
                and existing.fanned_out_to_subscribers
            ):
                # Honor the trader's "Cancel My Orders" intent. See
                # cancel_intent.py + the matching guard in trade_listener /
                # snaptrade_listener for the full rationale.
                from app.services.cancel_intent import consume_no_cascade  # noqa: PLC0415
                if consume_no_cascade(existing.id):
                    log.info(
                        "ibkr-listener[%s] suppressing cascade for order %s "
                        "— trader requested cancel-without-subscribers",
                        trader_user_id, existing.id,
                    )
                else:
                    _cascade_cancel_to_mirrors(existing.id)
            return

        # Brand-new order — only act on working/terminal-success states.
        if status_enum not in (
            OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        ):
            return

        order = _insert_order_from_ibkr(
            db, trader_user_id, broker_account_id, broker_order_id, order_obj, status_enum
        )
        order.trader_submitted_at = _as_dt(
            _attr(order_obj, "lastExecutionTime", "submittedTime", "time")
        )
        order.socket_received_at = datetime.now(timezone.utc)

        audit.record(
            db,
            actor_user_id=trader_user_id,
            action="listener.order_observed",
            entity_type="order",
            entity_id=order.id,
            metadata={
                "broker": "ibkr",
                "broker_order_id": broker_order_id,
                "status": status_str,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": str(order.quantity),
            },
        )
        order.redis_published_at = datetime.now(timezone.utc)

        # Replay guard — orders the trader placed BEFORE we started
        # watching this IBKR account are history surfaced by /iserver/
        # account/orders. Record them locally but DON'T mirror.
        acct = db.get(BrokerAccount, broker_account_id)
        if copy_engine.order_predates_connection(acct, order.trader_submitted_at):
            order.fanned_out_to_subscribers = True
            db.commit()
            db.refresh(order)
            events.publish(
                trader_user_id,
                copy_engine._order_event("order.placed", order),  # noqa: SLF001
            )
            log.info(
                "ibkr-listener[%s] skipping fanout — order %s predates connection",
                trader_user_id, broker_order_id,
            )
            return

        db.commit()
        db.refresh(order)
        events.publish(
            trader_user_id,
            copy_engine._order_event("order.placed", order),  # noqa: SLF001
        )

        # Dispatch fanout on the MAIN event loop (see copy_engine.fanout_threadsafe)
        # — never the throwaway asyncio.run loop that the sync fanout
        # would create. Keeps per-broker semaphores + the async Redis
        # client bound to a single stable loop.
        if _main_loop is not None:
            copy_engine.fanout_threadsafe(order.id, trader_user_id, _main_loop)
        else:
            trader = db.get(User, trader_user_id)
            if trader is not None:
                copy_engine.fanout(db, order, trader)
                order.fanned_out_to_subscribers = True
                db.commit()


def _insert_order_from_ibkr(
    db: Any,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    broker_order_id: str,
    order_obj: Any,
    status_enum: OrderStatus,
) -> Order:
    """Translate an IBKR order payload into our Order schema and INSERT.

    Stocks-only for now (matches the adapter's placement scope). Option
    detection lives in the contract description; once IBKR option
    placement is implemented we'll parse expiry/strike/right out here."""
    side_raw = str(_attr(order_obj, "side") or "").upper()
    side = _BUY if side_raw == "BUY" else _SELL

    type_raw = str(_attr(order_obj, "orderType") or "MKT").upper().replace(" ", "_")
    order_type = _TYPE_IN.get(type_raw, OrderType.MARKET)

    sec_type = (_attr(order_obj, "secType") or "").upper()
    instrument = (
        InstrumentType.OPTION if sec_type in ("OPT", "FOP") else InstrumentType.STOCK
    )
    # contractDesc for options reads like "AAPL 06JUN26 200 C"; first
    # token is the underlying ticker, which is what our Order.symbol
    # holds. For stocks it's just the ticker.
    symbol_raw = str(_attr(order_obj, "ticker", "symbol", "contractDesc") or "")
    symbol = symbol_raw.split(" ")[0].upper()

    qty = _to_dec(_attr(order_obj, "totalSize", "quantity")) or Decimal(0)
    limit_price = _to_dec(_attr(order_obj, "price"))
    stop_price = _to_dec(_attr(order_obj, "auxPrice", "stopPrice"))
    filled_q = _to_dec(_attr(order_obj, "filledQuantity", "cumQty")) or Decimal(0)
    filled_avg = _to_dec(_attr(order_obj, "avgPrice", "lastPrice"))
    submitted_at = (
        _as_dt(_attr(order_obj, "lastExecutionTime", "submittedTime", "time"))
        or datetime.now(timezone.utc)
    )

    order = Order(
        user_id=trader_user_id,
        broker_account_id=broker_account_id,
        instrument_type=instrument,
        symbol=symbol,
        # Option fields stay None for stocks; option placement is a TODO,
        # but if a trader places an option on IBKR's app directly we'll
        # detect it as instrument_type=OPTION with the underlying as
        # symbol — a follow-up will parse expiry/strike/right.
        option_expiry=None,
        option_strike=None,
        option_right=None,
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
        is_closing=False,
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
        log.exception("ibkr-listener cancel-cascade failed for %s", parent_order_id)


# ── Small helpers ───────────────────────────────────────────────────────────


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
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
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        sec = v / 1000.0 if v > 10**12 else float(v)
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
