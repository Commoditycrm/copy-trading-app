"""Alpaca trade_updates WebSocket listener.

Runs one task per active trader's connected Alpaca account. Picks up orders
the trader places **directly on Alpaca** (their dashboard / mobile / API)
and replays them through our DB + fanout machinery so subscribers receive
copies the same way they would for orders placed through our Trade Panel.

Design contract
---------------
- One asyncio task per (trader_user_id, broker_account_id).
- Uses ``alpaca.trading.stream.TradingStream`` which handles auth + pings.
- We wrap it with our own reconnect+backfill loop: on every (re)connection
  we poll ``/v2/account/activities`` for events that happened during the gap
  and replay them, so no trade is missed even if the listener was offline.
- Dedup: every Alpaca event carries the broker's ``order.id``. If we already
  have an ``Order`` row with that ``broker_order_id`` (e.g. because the
  trader placed the order through our Trade Panel), we only update its
  status; we DO NOT trigger another fanout. If the broker_order_id is new
  to us, the order was placed outside our app — we INSERT a fresh row and
  schedule fanout.
- Subscribers get the same SSE events and DB rows as today.

Not running on Vercel
---------------------
Long-lived WebSocket — requires a persistent process. Listener startup is
guarded so it's a no-op when the process is going down or already shutting
down. Designed for Lightsail / Render / Fly hosting.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import certifi
from alpaca.trading.stream import TradingStream

# Build an SSL context backed by certifi's CA bundle. macOS Python.org installs
# ship without a CA store, which makes the websockets handshake to
# wss://paper-api.alpaca.markets fail with "unable to get local issuer
# certificate". Using certifi explicitly avoids that whole class of problem.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from app.brokers.alpaca import _STATUS_IN, _parse_occ, _looks_like_occ
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
from app.services import audit, broker_filters, copy_engine, events, fills_sync, listener_state
from app.services.crypto import decrypt_json
from app.services.listener_state import ListenerState, ListenerStatus

log = logging.getLogger(__name__)


# ── Public state surface ────────────────────────────────────────────────────
#
# Status itself lives in ``listener_state._status`` so the Webull listener
# can share it (one-broker-per-user means only one writes at a time, but
# both publish via the same SSE event so the frontend stays broker-agnostic).
# We keep ``get_status``/``_set_state``/``_bump_last_event`` re-exported here
# for backwards compatibility with existing callers (api/listener.py etc.).

# Active asyncio tasks keyed by trader user_id. Cancelled on stop.
_tasks: dict[uuid.UUID, asyncio.Task] = {}

# Last known stream object per trader — we keep it so stop_listener can call
# stop_ws() before cancelling the task (cleaner shutdown).
_streams: dict[uuid.UUID, TradingStream] = {}

# Reference to the main FastAPI event loop, captured at app startup. We need
# this because start_listener() can be called from sync request handler
# threads (POST /api/brokers) — those threads have no event loop of their
# own, so we route create_task through the main loop via
# call_soon_threadsafe. Without this, the listener silently fails to start
# when a new broker is connected at runtime.
_main_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once from FastAPI startup so off-loop callers (sync request
    handlers) can schedule listener tasks on the right loop."""
    global _main_loop
    _main_loop = loop


# Thin re-exports so existing callers (api/listener.py and friends) keep
# working. The actual storage lives in listener_state — see that module
# for the rationale.
get_status = listener_state.get_status
_set_state = listener_state.set_state
_bump_last_event = listener_state.bump_last_event


# ── Lifecycle ───────────────────────────────────────────────────────────────


async def start_all_listeners() -> None:
    """Called from FastAPI lifespan on app startup. Walks the DB for every
    active TRADER user with a connected Alpaca account and spawns a listener
    task per (trader, account). Idempotent — safe to call multiple times."""
    with SessionLocal() as db:
        from sqlalchemy import select
        traders = db.execute(
            select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))
        ).scalars().all()
        for trader in traders:
            for acct in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader.id,
                    BrokerAccount.broker == BrokerName.ALPACA,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars():
                start_listener(trader.id, acct.id)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Spawn (or replace) the listener task for one (trader, account) pair.

    Safe to call from:
      - an async coroutine running on the FastAPI loop (uses get_running_loop)
      - a sync FastAPI handler thread (uses the loop captured by bind_loop
        at startup, scheduled via call_soon_threadsafe)
    """
    existing = _tasks.get(trader_user_id)
    if existing and not existing.done():
        # Already running for this trader. Restart cleanly so credentials
        # changes pick up.
        log.info("listener[%s] restart requested", trader_user_id)
        stop_listener(trader_user_id)

    # Prefer the running loop when we're already on it (avoids the
    # call_soon_threadsafe hop for the common startup path).
    try:
        loop = asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        loop = _main_loop
        on_loop = False

    if loop is None:
        # bind_loop() was never called — FastAPI startup didn't run yet, or
        # we're being imported from a context that has no main loop at all
        # (e.g. an alembic script). Nothing we can do.
        log.warning(
            "listener[%s] no main loop bound; start_listener is a no-op "
            "(did FastAPI startup run?)",
            trader_user_id,
        )
        return

    if on_loop:
        task = loop.create_task(_run_listener(trader_user_id, broker_account_id))
        _tasks[trader_user_id] = task
        _set_state(trader_user_id, "connecting")
    else:
        # Called from a sync handler thread — schedule task creation on the
        # main loop and store the resulting Task once it exists.
        def _schedule() -> None:
            task = loop.create_task(_run_listener(trader_user_id, broker_account_id))
            _tasks[trader_user_id] = task
            _set_state(trader_user_id, "connecting")

        loop.call_soon_threadsafe(_schedule)


def stop_listener(trader_user_id: uuid.UUID) -> None:
    """Signal the listener to shut down and clean up state."""
    stream = _streams.pop(trader_user_id, None)
    if stream is not None:
        try:
            stream.stop()
        except Exception:  # noqa: BLE001
            pass
    task = _tasks.pop(trader_user_id, None)
    if task and not task.done():
        task.cancel()
    _set_state(trader_user_id, "disconnected")


async def stop_all_listeners() -> None:
    """Called from FastAPI lifespan on shutdown."""
    for tid in list(_tasks.keys()):
        stop_listener(tid)


# ── Listener task ───────────────────────────────────────────────────────────


_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


async def _run_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Main loop for a single trader's listener. Reconnects forever (until
    cancelled) with exponential backoff; on each connection runs a backfill
    pass and then hands off to TradingStream.run_forever-equivalent."""
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            # Re-read creds + broker on every connect attempt — they may have
            # been rotated or the account marked disconnected since last try.
            creds, is_paper = _load_creds(trader_user_id, broker_account_id)
            if creds is None:
                _set_state(trader_user_id, "credentials_invalid",
                           error="broker disconnected or credentials missing")
                # Sleep before checking again — the trader may reconnect via UI.
                await asyncio.sleep(30)
                backoff = _BACKOFF_INITIAL
                continue

            stream = TradingStream(
                creds["api_key"],
                creds["api_secret"],
                paper=bool(is_paper),
                websocket_params={
                    "ping_interval": 10,
                    "ping_timeout": 180,
                    "max_queue": 1024,
                    "ssl": _SSL_CTX,
                },
            )
            _streams[trader_user_id] = stream

            async def handler(update: Any) -> None:  # bound to this trader
                await _handle_trade_update(trader_user_id, broker_account_id, update)

            stream.subscribe_trade_updates(handler)

            # Mark "connected" BEFORE the (potentially slow) backfill runs.
            # Backfill calls `_refresh_open_orders` + `list_recent_activities`
            # against Alpaca — for a trader with any meaningful history
            # that can take 10-30s per reconnect, and while it runs the
            # listener can't process anything else. The UI was reading
            # this as "broker connecting..." indefinitely. Move backfill
            # off the connect path: schedule it as an independent task
            # so it can run in parallel with the WebSocket consumer.
            _set_state(trader_user_id, "connected")
            backoff = _BACKOFF_INITIAL

            # Backfill anything we missed while disconnected. Cheap and safe;
            # idempotent because fills_sync dedupes by activity id. Runs in
            # parallel with stream consumption — if backfill is slow, it
            # doesn't block live event handling. Bounded by a hard 60s
            # timeout so a hung Alpaca call can't pile up tasks across
            # reconnects.
            asyncio.create_task(
                _background_backfill(trader_user_id, broker_account_id)
            )

            # Blocks until disconnected / cancelled.
            await stream._run_forever()  # noqa: SLF001 — public run() is sync

        except asyncio.CancelledError:
            log.info("listener[%s] cancelled", trader_user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("listener[%s] error: %s", trader_user_id, exc)
            _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])

        # Reconnect with exponential backoff capped at 60s.
        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


# Hard ceiling for how long a backfill task is allowed to run before we
# walk away from it. fills_sync can stall on slow Alpaca activity feeds —
# without this, a hung backfill from a previous reconnect could still be
# running by the time the next reconnect tries to schedule another one.
_BACKFILL_TIMEOUT_S = 60.0


async def _background_backfill(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> None:
    """Run fills_sync.sync_account_fills off the listener's main task.

    Failures (including timeouts) are logged and dropped — the listener
    keeps running and we'll retry on the next reconnect. Backfill is
    idempotent so a partial run is safe to replay.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_run_backfill, trader_user_id, broker_account_id),
            timeout=_BACKFILL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "listener[%s] backfill exceeded %.0fs — abandoning this round",
            trader_user_id,
            _BACKFILL_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        # If the listener task is cancelled (e.g. broker disconnect),
        # the backfill task created on the same loop gets cancelled too.
        # Don't log — this is expected cleanup.
        raise
    except Exception:  # noqa: BLE001
        log.exception("listener[%s] background backfill failed", trader_user_id)


def _load_creds(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID
) -> tuple[dict[str, Any] | None, bool]:
    """Return (creds_dict, is_paper) or (None, False) if the broker account
    is gone or disconnected."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if (
            acct is None
            or acct.user_id != trader_user_id
            or acct.broker != BrokerName.ALPACA
            or acct.connection_status != "connected"
        ):
            return None, False
        try:
            creds = decrypt_json(acct.encrypted_credentials)
        except Exception:  # noqa: BLE001
            return None, False
        return creds, bool(acct.is_paper)


# ── Message handler ─────────────────────────────────────────────────────────


_BUY = OrderSide.BUY
_SELL = OrderSide.SELL


async def _handle_trade_update(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    update: Any,
) -> None:
    """Per-event entry point. Defers DB + fanout to a thread so the async
    listener stays responsive."""
    _bump_last_event(trader_user_id)
    # Lifecycle: capture the moment our WS handler received the event,
    # BEFORE we hop to the threadpool. Used by the Performance page to
    # compute socket_lag (= socket_received_at - trader_submitted_at) for
    # externally-placed orders.
    socket_received_at = datetime.now(timezone.utc)
    try:
        await asyncio.to_thread(
            _persist_and_fanout,
            trader_user_id,
            broker_account_id,
            update,
            socket_received_at,
        )
    except Exception:  # noqa: BLE001
        log.exception("listener[%s] handler error", trader_user_id)


def _persist_and_fanout(
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    update: Any,
    socket_received_at: datetime,
) -> None:
    """Sync — runs in a thread. Saves the order to DB (insert or update) and
    triggers fanout when appropriate."""
    event_name = getattr(update.event, "value", str(update.event)).lower()
    alpaca_order = update.order
    broker_order_id = str(alpaca_order.id)

    # Gate: master switch + per-status (open vs filled) checkboxes on the
    # broker account. Re-read each event so the user can flip flags at
    # runtime and have it apply on the very next WS event. Treat anything
    # that isn't a final FILLED as "open lifecycle" for filter purposes.
    raw_status = str(getattr(alpaca_order, "status", "") or "").lower()
    status_for_gate = OrderStatus.FILLED if raw_status == "filled" else OrderStatus.SUBMITTED
    with SessionLocal() as gate_db:
        gate_acct = gate_db.get(BrokerAccount, broker_account_id)
        if not broker_filters.should_persist_order(gate_acct, status_for_gate):
            return

    with SessionLocal() as db:
        from sqlalchemy import select
        existing = db.execute(
            select(Order).where(Order.broker_order_id == broker_order_id)
        ).scalar_one_or_none()

        if existing is not None:
            # We already know about this order (Trade Panel placement, or an
            # earlier event for the same order). Just update status / fills.
            _apply_event_to_existing(db, existing, alpaca_order, update, event_name)
            # Lifecycle: stamp the first WS sighting of this order (only on
            # first event; later events keep the original timestamp so the
            # field reflects the *initial* notification latency).
            if existing.socket_received_at is None:
                existing.socket_received_at = socket_received_at
            existing.redis_published_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing)
            events.publish(
                trader_user_id, copy_engine._order_event("order.placed", existing)  # noqa: SLF001
            )
            # For an external trader cancel/close to propagate to subscribers,
            # we need to handle CANCEL via the same cascade as our internal
            # cancel endpoint when the original was a trader-originated order.
            if (
                event_name in ("canceled", "expired", "rejected")
                and existing.parent_order_id is None
                and existing.fanned_out_to_subscribers
            ):
                _cascade_cancel_to_mirrors(existing.id)
            return

        # Brand-new order: trader placed it on Alpaca directly, outside our app.
        # Only act on "new" / "fill" / "partial_fill" — terminal events on an
        # order we never saw start are ignored (we have nothing to mirror).
        if event_name not in ("new", "fill", "partial_fill", "accepted"):
            return

        order = _insert_order_from_alpaca(
            db, trader_user_id, broker_account_id, alpaca_order
        )

        # Lifecycle: for externally-placed orders we know two distinct
        # moments — when Alpaca itself accepted the order (alpaca_order
        # carries `submitted_at`) and when our WS handler heard about it.
        order.trader_submitted_at = getattr(alpaca_order, "submitted_at", None)
        order.socket_received_at = socket_received_at

        # Audit so the trail shows where the order came from.
        audit.record(
            db,
            actor_user_id=trader_user_id,
            action="listener.order_observed",
            entity_type="order",
            entity_id=order.id,
            metadata={
                "broker_order_id": broker_order_id,
                "event": event_name,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": str(order.quantity),
            },
        )
        # Lifecycle: stamp the broadcast moment before publishing.
        order.redis_published_at = datetime.now(timezone.utc)

        # Replay guard — don't mirror orders the trader placed before we
        # started watching this Alpaca account (history returned by the
        # backfill / first connect). See copy_engine.order_predates_connection.
        acct = db.get(BrokerAccount, broker_account_id)
        if copy_engine.order_predates_connection(acct, order.trader_submitted_at):
            order.fanned_out_to_subscribers = True
            db.commit()
            db.refresh(order)
            events.publish(
                trader_user_id, copy_engine._order_event("order.placed", order)  # noqa: SLF001
            )
            log.info(
                "listener[%s] skipping fanout — order %s predates connection",
                trader_user_id, order.broker_order_id,
            )
            return

        db.commit()
        db.refresh(order)

        events.publish(
            trader_user_id, copy_engine._order_event("order.placed", order)  # noqa: SLF001
        )

        # Per user's spec: partial fills mirror per partial. We treat every
        # "new"/"fill"/"partial_fill" event from an externally-placed order
        # as a fresh trade to mirror. To avoid mirroring twice for the same
        # external order, we set fanned_out_to_subscribers=True and dedupe on
        # subsequent events via the "existing" branch above.
        # Fan out on the main event loop (see copy_engine.fanout_threadsafe)
        # rather than a throwaway asyncio.run loop, so per-broker semaphores
        # and the async Redis client stay bound to one stable loop.
        if _main_loop is not None:
            copy_engine.fanout_threadsafe(order.id, trader_user_id, _main_loop)
        else:
            copy_engine.fanout(db, order, _load_trader(db, trader_user_id))
            order.fanned_out_to_subscribers = True
            db.commit()


def _apply_event_to_existing(
    db: Any, order: Order, alpaca_order: Any, update: Any, event_name: str
) -> None:
    """Update an existing Order row from a TradeUpdate event (status,
    filled_qty, filled_avg_price, closed_at)."""
    try:
        new_status = _STATUS_IN.get(alpaca_order.status, order.status)
    except Exception:  # noqa: BLE001
        new_status = order.status
    # Track whether THIS event flipped the status to FILLED — the
    # bracket-emulator hook below only fires on the transition, not on
    # every subsequent quiescent event for an already-FILLED order.
    status_changed_to_filled = (
        new_status == OrderStatus.FILLED and order.status != OrderStatus.FILLED
    )
    if new_status != order.status:
        order.status = new_status
    fq = getattr(alpaca_order, "filled_qty", None)
    if fq is not None:
        try:
            order.filled_quantity = Decimal(str(fq))
        except Exception:  # noqa: BLE001
            pass
    fap = getattr(alpaca_order, "filled_avg_price", None)
    if fap is not None:
        try:
            order.filled_avg_price = Decimal(str(fap))
        except Exception:  # noqa: BLE001
            pass
    if event_name in ("fill", "canceled", "expired", "rejected") and order.closed_at is None:
        order.closed_at = datetime.now(timezone.utc)

    # Bracket emulator hooks — mirror what snaptrade_listener / ibkr_listener
    # do on FILLED. Native bracket (Alpaca stocks) doesn't need this:
    # Alpaca itself attaches the TP/SL legs at submit and handles OCO
    # server-side, so emulate_bracket_exits short-circuits via
    # _uses_native_bracket(). The reason we still call it: Alpaca
    # OPTIONS reject complex orders (error 42210000), so api/trades.py
    # places those entries plain and the emulator is the only thing
    # that materialises the TP/SL exits when an option entry fills.
    # cancel_sibling_on_fill is the OCO half — when one emulator-placed
    # leg fills, cancel the surviving sibling. Both functions are no-ops
    # when the order doesn't match their case, so calling them together
    # is safe and saves duplicating the gate logic here.
    if status_changed_to_filled:
        try:
            from app.services.bracket_emulator import (  # noqa: PLC0415
                cancel_sibling_on_fill,
                emulate_bracket_exits,
            )
            emulate_bracket_exits(db, order)
            cancel_sibling_on_fill(db, order)
        except Exception:  # noqa: BLE001
            log.exception(
                "alpaca_listener: bracket emulator failed for order %s", order.id
            )


def _insert_order_from_alpaca(
    db: Any,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    alpaca_order: Any,
) -> Order:
    """Translate an Alpaca order resource into our Order schema and INSERT."""
    sym_full = str(alpaca_order.symbol or "")
    if _looks_like_occ(sym_full):
        parsed = _parse_occ(sym_full)
        if parsed is not None:
            display, expiry, strike, right = parsed
            instrument = InstrumentType.OPTION
            symbol = display
            option_expiry = expiry
            option_strike = strike
            option_right = right
        else:
            instrument = InstrumentType.STOCK
            symbol = sym_full.upper()
            option_expiry = option_strike = option_right = None
    else:
        instrument = InstrumentType.STOCK
        symbol = sym_full.upper()
        option_expiry = option_strike = option_right = None

    side_raw = str(getattr(alpaca_order.side, "value", alpaca_order.side)).lower()
    side = _BUY if side_raw == "buy" else _SELL

    type_raw = str(getattr(alpaca_order.order_type, "value", alpaca_order.order_type)).lower()
    order_type = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "stop": OrderType.STOP,
        "stop_limit": OrderType.STOP_LIMIT,
    }.get(type_raw, OrderType.MARKET)

    qty = Decimal(str(alpaca_order.qty)) if alpaca_order.qty is not None else Decimal(0)
    limit_price = Decimal(str(alpaca_order.limit_price)) if getattr(alpaca_order, "limit_price", None) else None
    stop_price = Decimal(str(alpaca_order.stop_price)) if getattr(alpaca_order, "stop_price", None) else None
    filled_q = Decimal(str(getattr(alpaca_order, "filled_qty", 0) or 0))
    filled_avg = (
        Decimal(str(alpaca_order.filled_avg_price))
        if getattr(alpaca_order, "filled_avg_price", None)
        else None
    )
    submitted_at = getattr(alpaca_order, "submitted_at", None) or datetime.now(timezone.utc)
    status = _STATUS_IN.get(alpaca_order.status, OrderStatus.SUBMITTED)

    order = Order(
        user_id=trader_user_id,
        broker_account_id=broker_account_id,
        instrument_type=instrument,
        symbol=symbol,
        option_expiry=option_expiry,
        option_strike=option_strike,
        option_right=option_right,
        side=side,
        order_type=order_type,
        quantity=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        status=status,
        broker_order_id=str(alpaca_order.id),
        filled_quantity=filled_q,
        filled_avg_price=filled_avg,
        submitted_at=submitted_at,
        closed_at=(
            datetime.now(timezone.utc) if status in (
                OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
            ) else None
        ),
        # The listener stamps this once fanout runs (a few lines below in the
        # caller); set to False initially so dedup short-circuit works.
        fanned_out_to_subscribers=False,
    )
    db.add(order)
    db.flush()
    return order


def _load_trader(db: Any, trader_user_id: uuid.UUID) -> User:
    return db.get(User, trader_user_id)


def _cascade_cancel_to_mirrors(parent_order_id: uuid.UUID) -> None:
    """When an externally-placed trader order is cancelled, cancel its open
    mirrors too — same semantics as our internal cancel endpoint."""
    # Reuse the existing background helper. Imported lazily to avoid a cycle.
    from app.api.trades import _run_cancel_fanout_in_background
    try:
        _run_cancel_fanout_in_background(parent_order_id)
    except Exception:  # noqa: BLE001
        log.exception("listener cancel-cascade failed for %s", parent_order_id)


# ── Backfill ────────────────────────────────────────────────────────────────


def _run_backfill(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Sync — runs in a thread. Pulls Alpaca activities for this trader's
    account and upserts fills. Catches orders placed while the listener was
    offline. After the upsert runs a one-shot fanout pass over any
    backfilled parent orders that haven't been broadcast yet — so
    subscribers receive copies of trades the trader placed while the
    listener was offline.

    Idempotent — ``fanned_out_to_subscribers=True`` after fanout prevents
    double-fanout on subsequent backfill cycles, and ``copy_engine.fanout``
    itself dedupes per child broker_account.
    """
    from sqlalchemy import select  # local — avoid top-level cycle risk

    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if acct is None:
            return
        try:
            fills_sync.sync_account_fills(db, acct)
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("listener backfill failed for %s", broker_account_id)
            db.rollback()
            return

        # ── One-shot fanout for orders missed while the listener was offline ──
        # Without this pass, trades placed externally on Alpaca while we were
        # disconnected sit in the DB with fanned_out_to_subscribers=False
        # forever; subscribers never receive copies and the Performance page
        # stays empty. Only fanout orders in working/terminal-success states
        # — PENDING/REJECTED parents shouldn't be broadcast.
        trader = db.get(User, trader_user_id)
        if trader is None:
            return
        unfanned = db.execute(
            select(Order).where(
                Order.user_id == trader_user_id,
                Order.parent_order_id.is_(None),
                Order.fanned_out_to_subscribers.is_(False),
                Order.status.in_(
                    [
                        OrderStatus.SUBMITTED,
                        OrderStatus.ACCEPTED,
                        OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.FILLED,
                    ]
                ),
            )
        ).scalars().all()
        if not unfanned:
            return

        acct = db.get(BrokerAccount, broker_account_id)
        fanned = 0
        skipped_historical = 0
        for o in unfanned:
            # Replay guard — orders placed before we started watching this
            # broker are history; mark them resolved without mirroring.
            if copy_engine.order_predates_connection(acct, o.trader_submitted_at or o.submitted_at):
                o.fanned_out_to_subscribers = True
                skipped_historical += 1
                continue
            try:
                if _main_loop is not None:
                    copy_engine.fanout_threadsafe(o.id, trader_user_id, _main_loop)
                else:
                    copy_engine.fanout(db, o, trader)
                    o.fanned_out_to_subscribers = True
                fanned += 1
            except Exception:  # noqa: BLE001
                log.exception(
                    "listener backfill fanout failed for order %s", o.id
                )
        if skipped_historical:
            log.info(
                "listener[%s] backfill skipped %d historical orders (pre-connection)",
                trader_user_id, skipped_historical,
            )
        try:
            db.commit()
            log.info(
                "listener[%s] backfilled-fanout %d/%d orders",
                trader_user_id,
                fanned,
                len(unfanned),
            )
        except Exception:  # noqa: BLE001
            log.exception("listener backfill fanout commit failed")
            db.rollback()
