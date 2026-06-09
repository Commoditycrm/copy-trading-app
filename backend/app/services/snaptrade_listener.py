"""SnapTrade order-update listener — polling-based.

Why slower than Webull
----------------------
SnapTrade itself polls the upstream broker on roughly a 5–30s cadence
(varies per broker). Polling on our side faster than that is wasted
work — we just see the same SnapTrade snapshot multiple times. We poll
every ``POLL_INTERVAL_S`` (5s default) which is a fair tradeoff between
freshness and SnapTrade rate-limit headroom.

End-to-end latency: 5–60s from the trader's actual fill to subscribers
seeing the mirror order. That's the architectural cost of going
through an aggregator — there's no fix for it short of switching that
trader to a direct broker integration.

Otherwise mirrors the public surface of ``trade_listener.py`` and
``webull_listener.py`` so the same shared ``listener_state`` powers the
SSE pill regardless of broker.
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

from app.brokers.snaptrade import SnapTradeAdapter, parse_snaptrade_order_symbol
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


# Per the module docstring: SnapTrade's own upstream poll cadence sets a
# floor on useful freshness. 5s is a fine default for the self-poller.
POLL_INTERVAL_S = 5.0
# When a webhook secret is configured, SnapTrade's Trade Detection +
# webhook becomes the primary trigger and our self-poll is only a
# backstop — so we slow it down to avoid redundant API calls.
POLL_INTERVAL_BACKSTOP_S = 60.0


_tasks: dict[uuid.UUID, asyncio.Task] = {}
_last_seen: dict[uuid.UUID, dict[str, str]] = {}
# Per-trader lock so a webhook-triggered immediate poll and the periodic
# poll can't run _poll_once concurrently for the same trader (which could
# double-insert a brand-new order — there's no DB unique constraint on
# broker_order_id, the dedup is a SELECT-then-INSERT inside _poll_once).
_poll_locks: dict[uuid.UUID, threading.Lock] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


def _lock_for(trader_user_id: uuid.UUID) -> "threading.Lock":
    return _poll_locks.setdefault(trader_user_id, threading.Lock())


def _poll_interval() -> float:
    """5s normally; 60s backstop when a webhook drives detection."""
    from app.config import get_settings
    return POLL_INTERVAL_BACKSTOP_S if get_settings().snaptrade_webhook_enabled else POLL_INTERVAL_S


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


# Re-exports — same shape as the other listeners.
get_status = listener_state.get_status
_set_state = listener_state.set_state


# ── Lifecycle ───────────────────────────────────────────────────────────────


async def start_all_listeners() -> None:
    """On app startup, spawn a poll task for every active TRADER with a
    connected SnapTrade account."""
    with SessionLocal() as db:
        traders = db.execute(
            select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))
        ).scalars().all()
        for trader in traders:
            for acct in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader.id,
                    BrokerAccount.broker == BrokerName.SNAPTRADE,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars():
                start_listener(trader.id, acct.id)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    existing = _tasks.get(trader_user_id)
    if existing and not existing.done():
        log.info("snaptrade-listener[%s] restart requested", trader_user_id)
        stop_listener(trader_user_id)

    try:
        loop = asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        loop = _main_loop
        on_loop = False

    if loop is None:
        log.warning(
            "snaptrade-listener[%s] no main loop bound; start_listener is a no-op",
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


# ── Poll task ───────────────────────────────────────────────────────────────


_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


async def _run_listener(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> None:
    """Outer loop: load creds → verify → inner poll loop → reconnect.
    Same shape as webull_listener._run_listener."""
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

            adapter = SnapTradeAdapter(creds)
            # First connect: hit balance to confirm the SnapTrade auth is
            # still valid. SnapTrade authorizations are revoked when the
            # underlying broker session ends (e.g. user changed their
            # Robinhood password) — we surface that as credentials_invalid.
            try:
                await asyncio.to_thread(adapter.verify_connection)
            except Exception as exc:  # noqa: BLE001
                _set_state(trader_user_id, "credentials_invalid", error=str(exc)[:300])
                await asyncio.sleep(60)
                continue

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
                        "snaptrade-listener[%s] poll iteration failed", trader_user_id
                    )
                    _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])
                    break
                await asyncio.sleep(_poll_interval())

        except asyncio.CancelledError:
            log.info("snaptrade-listener[%s] cancelled", trader_user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("snaptrade-listener[%s] error: %s", trader_user_id, exc)
            _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])

        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


# ── Webhook-triggered immediate poll ────────────────────────────────────────


async def poll_now_for_trader(trader_user_id: uuid.UUID) -> bool:
    """Run one poll immediately for this trader, outside the periodic
    loop. Called by the SnapTrade Trade-Detection webhook so a new order
    is picked up the instant SnapTrade notifies us, instead of waiting
    for the next periodic tick.

    Returns True if a poll ran, False if the trader has no connected
    SnapTrade account or the poll errored. Shares ``_last_seen`` + the
    per-trader lock with the periodic loop, so it's safe to run
    concurrently — the lock serialises the SELECT-then-INSERT in
    _poll_once. Exception-safe because it runs as a fire-and-forget
    background task from the webhook handler."""
    try:
        with SessionLocal() as db:
            acct = db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader_user_id,
                    BrokerAccount.broker == BrokerName.SNAPTRADE,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalar_one_or_none()
            if acct is None:
                return False
            broker_account_id = acct.id

        creds = _load_creds(trader_user_id, broker_account_id)
        if creds is None:
            return False
        adapter = SnapTradeAdapter(creds)
        await asyncio.to_thread(_poll_once, trader_user_id, broker_account_id, adapter)
        return True
    except Exception:  # noqa: BLE001
        log.exception("snaptrade poll_now_for_trader failed for %s", trader_user_id)
        return False


# ── Credential helpers ──────────────────────────────────────────────────────


def _load_creds(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> dict[str, Any] | None:
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if (
            acct is None
            or acct.user_id != trader_user_id
            or acct.broker != BrokerName.SNAPTRADE
            or acct.connection_status != "connected"
        ):
            return None
        try:
            return decrypt_json(acct.encrypted_credentials)
        except Exception:  # noqa: BLE001
            log.exception(
                "snaptrade-listener[%s] failed to decrypt credentials", trader_user_id
            )
            return None


# ── Poll iteration ──────────────────────────────────────────────────────────


_BUY = OrderSide.BUY
_SELL = OrderSide.SELL


def _poll_once(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    adapter: SnapTradeAdapter,
) -> None:
    """Pull recent orders, diff against last-seen, route changes through
    the persist+fanout pipeline. Sync — runs in a thread.

    Guarded by a per-trader lock so the periodic loop and a
    webhook-triggered poll (poll_now_for_trader) never race on the
    SELECT-then-INSERT dedup inside _persist_and_fanout."""
    with _lock_for(trader_user_id):
        # Gate: master switch off → skip the broker fetch + processing.
        # Reload each poll so the flag can be flipped at runtime via the
        # Brokers-page "Auto Pull Orders" checkbox.
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
            broker_order_id = str(_attr(o, "brokerage_order_id", "id", default=""))
            if not broker_order_id:
                continue
            status_str = str(_attr(o, "status", default="")).upper()
            prev = seen.get(broker_order_id)
            if prev == status_str:
                continue
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
    from app.brokers.snaptrade import _STATUS_IN as SNAP_STATUS_IN

    status_enum = SNAP_STATUS_IN.get(status_str, OrderStatus.SUBMITTED)

    with SessionLocal() as db:
        # Per-order gate: respect bring_open_orders / bring_filled_orders
        # so a flip mid-stream (e.g. user unchecks "Bring Filled orders"
        # at runtime) takes effect on the very next observed event.
        acct_gate = db.get(BrokerAccount, broker_account_id)
        if not broker_filters.should_persist_order(acct_gate, status_enum):
            return
        # Scope the lookup to *this trader's own* order by (user_id,
        # broker_order_id) — NOT broker_account_id. Reconnecting a broker
        # deletes the old broker_account, and the FK (ondelete=SET NULL) then
        # nulls broker_account_id on all its historical orders. If we scoped by
        # broker_account_id we'd miss those orphaned rows and re-insert the same
        # order under the new account on every reconnect (the source of the
        # duplicate pile-up). Matching by user_id finds the orphaned row so we
        # update + re-adopt it instead.
        #
        # parent_order_id IS NULL excludes subscriber mirror orders (which carry
        # their own broker_order_id). broker_order_id has no unique constraint,
        # so use LIMIT 1 + .first() — a dup group can never raise
        # MultipleResultsFound and crash the poll loop; deterministic order_by
        # keeps the chosen row stable across polls.
        existing = db.execute(
            select(Order)
            .where(Order.broker_order_id == broker_order_id)
            .where(Order.user_id == trader_user_id)
            .where(Order.parent_order_id.is_(None))
            .order_by(Order.created_at)
            .limit(1)
        ).scalars().first()

        if existing is not None:
            # Re-adopt an orphaned row (broker_account_id nulled by a prior
            # reconnect) back onto the live account so it stays linked.
            if existing.broker_account_id != broker_account_id:
                existing.broker_account_id = broker_account_id
            # Track whether *this* poll observed a status transition — the
            # bracket emulator hooks below only fire on actual transitions
            # so we don't pay for them on every quiescent poll of an
            # already-FILLED order.
            status_changed = existing.status != status_enum
            if status_changed:
                existing.status = status_enum
            fq = _attr(order_obj, "filled_units", "filled_quantity")
            if fq is not None:
                try:
                    existing.filled_quantity = Decimal(str(fq))
                except Exception:  # noqa: BLE001
                    pass
            fap = _attr(order_obj, "execution_price", "filled_avg_price")
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
            # Bracket emulator hooks. We run BOTH on every FILLED
            # transition; each function short-circuits if the order
            # doesn't match its case (entry vs exit leg), so calling them
            # together is safe and saves duplicating the gate logic here.
            # Failures are caught and logged — we don't want a bracket
            # bug to corrupt the listener's main status update.
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
                        "snaptrade_listener: bracket emulator failed for order %s",
                        existing.id,
                    )
            db.commit()
            db.refresh(existing)
            events.publish(
                trader_user_id,
                copy_engine._order_event("order.placed", existing),  # noqa: SLF001
            )
            if (
                status_str.upper() in ("CANCELLED", "CANCELED", "EXPIRED", "REJECTED", "FAILED")
                and existing.parent_order_id is None
                and existing.fanned_out_to_subscribers
            ):
                _cascade_cancel_to_mirrors(existing.id)
            return

        # Brand-new order — only act on working/terminal-success states.
        if status_enum not in (
            OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        ):
            return

        order = _insert_order_from_snaptrade(
            db, trader_user_id, broker_account_id, broker_order_id, order_obj, status_enum
        )

        # Lifecycle stamps. `socket_received_at` is reused for poll-time
        # so the Performance page can report "broker → us" latency in
        # one column regardless of transport.
        order.trader_submitted_at = _as_dt(_attr(order_obj, "time_placed", "created_at"))
        order.socket_received_at = datetime.now(timezone.utc)

        audit.record(
            db,
            actor_user_id=trader_user_id,
            action="listener.order_observed",
            entity_type="order",
            entity_id=order.id,
            metadata={
                "broker": "snaptrade",
                "broker_order_id": broker_order_id,
                "status": status_str,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": str(order.quantity),
            },
        )
        order.redis_published_at = datetime.now(timezone.utc)

        # Replay guard: if this order was placed before we started
        # watching this broker, it's history surfaced by SnapTrade's
        # recent-orders list — record it but DON'T mirror it to
        # subscribers. Marking fanned_out_to_subscribers=True means
        # "fanout resolved" so it's never retried.
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
                "snaptrade-listener[%s] skipping fanout — order %s predates connection",
                trader_user_id, broker_order_id,
            )
            return

        db.commit()
        db.refresh(order)

        events.publish(
            trader_user_id,
            copy_engine._order_event("order.placed", order),  # noqa: SLF001
        )

        # Fan out on the main event loop (see copy_engine.fanout_threadsafe)
        # instead of a throwaway asyncio.run loop, so per-broker semaphores
        # and the async Redis client stay bound to one stable loop. Without
        # this, the second detected order hits a cross-loop error and the
        # mirror silently fails.
        if _main_loop is not None:
            copy_engine.fanout_threadsafe(order.id, trader_user_id, _main_loop)
        else:
            trader = db.get(User, trader_user_id)
            if trader is not None:
                copy_engine.fanout(db, order, trader)
                order.fanned_out_to_subscribers = True
                db.commit()


def _insert_order_from_snaptrade(
    db: Any,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    broker_order_id: str,
    order_obj: Any,
    status_enum: OrderStatus,
) -> Order:
    """Translate a SnapTrade order payload into our Order schema and INSERT.

    Detects whether the order is a stock or an option via the SnapTrade
    symbol payload — see ``parse_snaptrade_order_symbol`` for the shape.
    Without this routing, options inserted as stocks won't surface in
    Option Haven's option views (and would have meaningless symbol +
    missing expiry/strike/right fields)."""
    parsed = parse_snaptrade_order_symbol(order_obj)

    # SnapTrade option actions are BUY_TO_OPEN / BUY_TO_CLOSE /
    # SELL_TO_OPEN / SELL_TO_CLOSE. We collapse them to our two-value
    # OrderSide (BUY/SELL) and use the _TO_CLOSE half to set is_closing,
    # which the Order Haven UI uses to render closing-trade pills.
    side_raw = str(_attr(order_obj, "action", default="")).upper()
    side = _BUY if "BUY" in side_raw else _SELL
    is_closing = "CLOSE" in side_raw

    type_raw = str(_attr(order_obj, "order_type", default="")).capitalize()
    order_type = {
        "Market":    OrderType.MARKET,
        "Limit":     OrderType.LIMIT,
        "Stop":      OrderType.STOP,
        "Stoplimit": OrderType.STOP_LIMIT,
    }.get(type_raw, OrderType.MARKET)

    qty = _to_dec(_attr(order_obj, "total_quantity", "units")) or Decimal(0)
    limit_price = _to_dec(_attr(order_obj, "limit_price", "price"))
    stop_price = _to_dec(_attr(order_obj, "stop_price", "stop"))
    filled_q = _to_dec(_attr(order_obj, "filled_units", "filled_quantity")) or Decimal(0)
    filled_avg = _to_dec(_attr(order_obj, "execution_price", "filled_avg_price"))
    submitted_at = (
        _as_dt(_attr(order_obj, "time_placed", "created_at"))
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
        is_closing=is_closing,
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
        log.exception("snaptrade-listener cancel-cascade failed for %s", parent_order_id)


# ── Small helpers ───────────────────────────────────────────────────────────


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
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
