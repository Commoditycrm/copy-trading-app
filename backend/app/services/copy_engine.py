"""Copy-trade fan-out (direct broker, async parallel execution).

When the trader places an order, fan out to every active subscriber's broker
account, scaled by their multiplier. Quantity rounding rule:
  - If broker supports fractional shares: keep raw multiplied quantity (truncated to 6dp).
  - Otherwise: floor to whole shares. If result is 0, skip and audit-log the skip.

Execution model (async):
  Phase 1 (serial, fast): for each subscriber × broker_account, compute the
                          scaled qty, insert a child Order row in PENDING state.
                          Subscribers + broker accounts come from the Redis
                          cache when warm.
  Phase 2 (parallel, async): fire all broker calls concurrently using
                            asyncio.gather. Sync broker SDKs are wrapped in
                            asyncio.to_thread so they don't block the loop.
                            Per-broker asyncio.Semaphore caps concurrency to
                            respect rate limits.
  Phase 3 (serial): apply the broker responses back to the child Order rows
                    and audit-log each result. Publish an SSE event per
                    subscriber so their UI updates immediately.

A failure on one subscriber must NOT block the others — handled by
return_exceptions=True on gather + per-task exception capture.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, BrokerOrderResult, adapter_for
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services import audit, cache, events
from app.services import market_hours
from app.services.platform_config import get_fanout_batch_threshold_async
from app.services.crypto import decrypt_json
from app.services.order_retry import (
    classify_error,
    clean_broker_error,
    is_order_conflict_error,
    is_rate_limit_error,
    is_replace_chain_pending_error,
    live_closeable_quantity,
)
from app.services.pnl import today_realized_pnl, today_realized_pnl_bulk

log = logging.getLogger(__name__)


# ── Historical-order replay guard ───────────────────────────────────────────
#
# When a listener (Alpaca WS / Webull poll / SnapTrade poll) first attaches to
# a trader's broker, the broker's API returns the trader's RECENT order
# history — not just brand-new orders. Without a guard we'd treat all of that
# history as fresh trades and fan it out to every subscriber, dumping stale
# orders onto their (possibly real-money) accounts the moment they connect.
#
# The guard: only mirror orders the trader placed AFTER we started watching
# their broker — i.e. after the BrokerAccount row's created_at. Anything older
# is historical and is recorded locally but NOT fanned out.

# Grace window for clock skew / a trade placed in the same minute the broker
# was connected. Generous on purpose — better to mirror one borderline order
# than to drop a genuine just-placed trade.
FANOUT_HISTORICAL_GRACE_S = 120


def order_predates_connection(
    broker_account: BrokerAccount | None,
    order_placed_at: datetime | None,
) -> bool:
    """True if this listener-detected order was placed before we began
    watching the trader's broker (so it's history and must NOT be
    mirrored). Compares the order's broker-side placement time against
    ``broker_account.created_at`` minus a grace window.

    Fail-open (returns False → allow fanout) when either timestamp is
    missing: dropping a real just-placed trade is worse for copy-trading
    than occasionally mirroring one borderline historical order. In
    practice every broker supplies a placement time, and historical
    orders all carry real (old) timestamps, so the bulk-replay case is
    reliably caught."""
    if order_placed_at is None or broker_account is None or broker_account.created_at is None:
        return False
    placed = order_placed_at if order_placed_at.tzinfo else order_placed_at.replace(tzinfo=timezone.utc)
    created = broker_account.created_at
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    watermark = created - timedelta(seconds=FANOUT_HISTORICAL_GRACE_S)
    return placed < watermark


# Map subscriber's RetryInterval enum value → wall-clock minutes to wait
# before the retry_scheduler picks the order back up.
_RETRY_INTERVAL_MINUTES: dict[RetryInterval, int] = {
    RetryInterval.ONE_M: 1,
    RetryInterval.TWO_M: 2,
    RetryInterval.THREE_M: 3,
    RetryInterval.FIVE_M: 5,
}

# Per-broker semaphores. Lazily created on the running event loop so they
# bind to the right loop (FastAPI's). Sized from settings.
_BROKER_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _broker_sem(broker: BrokerName) -> asyncio.Semaphore:
    key = broker.value if isinstance(broker, BrokerName) else str(broker)
    sem = _BROKER_SEMAPHORES.get(key)
    if sem is None:
        s = get_settings()
        # Default 32 for any broker without an explicit knob.
        limit = getattr(s, f"broker_concurrency_{key}", 32)
        sem = asyncio.Semaphore(limit)
        _BROKER_SEMAPHORES[key] = sem
    return sem


# Threading counterpart of _broker_sem for the SYNC threadpool fanout paths
# (propagate_modify_to_mirrors / cancel_and_replace_mirrors_for_modify /
# force_fill_mirrors_to_market). Those run place/cancel/replace in a
# ThreadPoolExecutor(max_workers=32), which the asyncio Semaphore can't gate —
# so without this they burst up to 32 concurrent calls per broker and trip
# SnapTrade's place-order 429. Same per-broker config knob as _broker_sem.
_BROKER_THREAD_SEMAPHORES: dict[str, threading.Semaphore] = {}
_BROKER_THREAD_SEM_LOCK = threading.Lock()

# Inline retry for a broker RATE-LIMIT throttle (SnapTrade 429 "Request was
# throttled. Expected available in 1 second."). Even under the concurrency cap,
# SnapTrade still 429s some place_mleg_order calls on a burst; a 429 means the
# order was NOT placed, so we wait out the throttle and retry — ALWAYS, not only
# for subscribers who opted into retries — so a 1s throttle can't turn a close
# into a REJECTED (prod QQQ 2026-08-11 stranded a subscriber long). Backoff grows
# per attempt to spread the retries and avoid immediately re-throttling.
_RATE_LIMIT_ATTEMPTS = 4
_RATE_LIMIT_BACKOFF_S = 1.1


def _broker_thread_sem(broker_key: str) -> threading.Semaphore:
    sem = _BROKER_THREAD_SEMAPHORES.get(broker_key)
    if sem is None:
        with _BROKER_THREAD_SEM_LOCK:
            sem = _BROKER_THREAD_SEMAPHORES.get(broker_key)  # re-check under lock
            if sem is None:
                limit = getattr(get_settings(), f"broker_concurrency_{broker_key}", 32)
                sem = threading.Semaphore(limit)
                _BROKER_THREAD_SEMAPHORES[broker_key] = sem
    return sem


def _throttled_item(fn, item):
    """Run one per-mirror threadpool task under its broker's concurrency cap so
    the sync fanout paths don't burst SnapTrade. The adapter is item[1] in every
    fanout tuple ((child, adapter, req) or (child, adapter, req, new_id))."""
    ad = item[1]
    with _broker_thread_sem(getattr(ad, "name", "") or ""):
        return fn(item)


@dataclass
class FanoutResult:
    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID
    order_id: uuid.UUID | None
    status: str       # "submitted" | "skipped_zero_qty" | "skipped_no_broker" | "error"
    detail: str | None = None


@dataclass
class _PendingMirror:
    """Phase-1 output: a child Order row already inserted, plus a constructed
    adapter ready to place. We resolve the adapter in phase 1 (one DB read for
    credentials) so phase 2 can be pure parallel HTTP."""
    child_order_id: uuid.UUID
    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID
    broker: BrokerName
    adapter: Any                                # BrokerAdapter, pre-built
    request: BrokerOrderRequest
    # True when the TRADER's own order is already FILLED at the moment we mirror.
    # Only then do we FORCE a close to fill immediately (see
    # _place_mirror_with_conflict_resolve). While the trader's close is still
    # working we mirror their limit as-is so the subscriber rests a cancellable
    # order — and Part B sweeps it to market if the trader's close later fills.
    trader_filled: bool = False
    # The trader's own fill price (filled_avg_price) when trader_filled — used to
    # anchor an extended-hours marketable LIMIT to where the trader actually
    # traded, since our own quote can diverge from the trader's venue pre-market
    # (see _to_immediate_close / _ext_hours_limit_price). None when not filled.
    trader_fill_price: Decimal | None = None


def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    # WHOLE SHARES ONLY (product decision, 2026-08): copy trades are never
    # fractional, even on brokers that support it. Always round the scaled size
    # DOWN to a whole unit — so 3 × 0.5 = 1.5 → 1. A size that rounds to 0 (e.g.
    # 1 × 0.5 = 0.5 → 0) yields no mirror (recorded as copy.skipped_zero_qty).
    # ``fractional`` is kept for call-site compatibility but intentionally ignored.
    return (trader_qty * multiplier).to_integral_value(rounding=ROUND_DOWN)


class _DanglingEntryCancelled(Exception):
    """Raised inside ``_place_mirror_with_conflict_resolve`` when the trader
    CLOSED a position but the subscriber holds NOTHING — their entry never
    filled (its mirror BUY is still working, or already gone). There's nothing
    to sell, and a naked SELL would just reject as a short/wash conflict. So we
    cancel the dangling working entry (so it can't fill LATER into a position
    the trader already exited) and report the mirror as CANCELED — no retry.

    ``cancelled_ids`` are the working entry rows we cancelled at the broker."""

    def __init__(self, cancelled_ids: list[uuid.UUID]):
        super().__init__("trader closed before subscriber entry filled; entry cancelled")
        self.cancelled_ids = cancelled_ids


class _DeferUntilEntryFills(Exception):
    """Raised inside ``_place_mirror_with_conflict_resolve`` when a close can't be
    placed YET because the subscriber's own ENTRY for this contract is still
    working (unfilled) — e.g. a pre-market BUY queued for the 09:30 open, or a
    fast scalp where the SELL arrives before the BUY has filled. Placing the SELL
    now just rejects (Alpaca "opposite side order exists" wash-trade / SnapTrade
    "no position to close"). Instead we DEFER: park the close as RETRY_PENDING and
    fire it the moment the entry fills (see fire_deferred_closes_for_entry, called
    from the fill-detection paths). The close must NOT be thrown away — it's valid,
    just not yet."""

    def __init__(self, entry_ids: list[uuid.UUID]):
        super().__init__("close deferred until subscriber entry fills")
        self.entry_ids = entry_ids


class _OpeningShortSkipped(Exception):
    """Raised inside ``_place_mirror_with_conflict_resolve`` when a trader's SELL
    reaches a subscriber who holds NOTHING to close (and has no working entry),
    so placing it would open a NAKED SHORT. Almost always an unintended short
    from a missed/mis-synced entry — SnapTrade rejects it, but Alpaca would
    actually short the account (prod: HUIZ shorted two subscribers). We skip it
    instead. Suppressed only when settings.copy_allow_opening_shorts is true."""


class _KeptProtectiveStop(Exception):
    """Raised inside ``_place_mirror_with_conflict_resolve`` when a NON-stop mirror
    close (a resting take-profit LIMIT) collides with the subscriber's existing
    working STOP-loss on the same position. On Alpaca a position's shares back only
    ONE resting sell, so the two can't coexist — and the naive conflict-resolver
    would cancel the blocker, which is the STOP, silently removing downside
    protection (observed on prod STKH, 2026-07-28). Instead we KEEP the stop and
    skip this order. The subscriber still exits when the trader's take-profit fills
    (fill-driven close). ``kept_stop_ids`` are the protective stop rows left working."""

    def __init__(self, kept_stop_ids: list[uuid.UUID]):
        super().__init__("kept protective stop-loss; conflicting take-profit not placed")
        self.kept_stop_ids = kept_stop_ids


# How long a deferred close waits before the retry-scheduler gives up on it (a
# safety net if the entry-fill event is ever missed). Comfortably spans
# pre-market → close so a 09:30 fill always fires the event-driven path first.
_DEFERRED_CLOSE_TTL_HOURS = 8


# Statuses whose UNFILLED remainder still reserves shares at the broker
# (the broker's "held_for_orders"). A second close of the same shares while
# one of these is working gets rejected (e.g. Alpaca 40310000).
_WORKING_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)

# Order types that rest as a PROTECTIVE stop-loss. On Alpaca a position's shares
# back only ONE resting sell, so a trader who rests BOTH a stop-loss and a
# take-profit on the same position collides — and the conflict-resolve path must
# not sacrifice the stop (downside protection) to place the take-profit. See
# _KeptProtectiveStop / _working_protective_stop_ids.
_PROTECTIVE_STOP_TYPES = (OrderType.STOP, OrderType.STOP_LIMIT)


def _cancel_subscriber_conflicts(item: "_PendingMirror") -> list[uuid.UUID]:
    """Cancel the subscriber's still-working orders for the SAME contract as the
    mirror close in ``item`` — the ones blocking it (wash trade / uncovered /
    insufficient qty). Cancels at the subscriber's broker and marks each CANCELED
    (its own session — this runs in a worker thread). Returns cancelled ids."""
    req = item.request
    cancelled: list[uuid.UUID] = []
    with SessionLocal() as db:
        rows = db.execute(
            select(Order).where(
                Order.user_id == item.subscriber_user_id,
                Order.broker_account_id == item.broker_account_id,
                Order.instrument_type == req.instrument_type,
                Order.symbol == req.symbol,
                Order.option_expiry.is_not_distinct_from(req.option_expiry),
                Order.option_strike.is_not_distinct_from(req.option_strike),
                Order.option_right.is_not_distinct_from(req.option_right),
                Order.status.in_(_WORKING_ORDER_STATUSES),
                Order.broker_order_id.isnot(None),
                Order.id != item.child_order_id,
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        published: list[Order] = []
        for o in rows:
            try:
                item.adapter.cancel_order(o.broker_order_id)
            except Exception:  # noqa: BLE001
                log.warning(
                    "copy: failed to cancel conflicting order %s (broker_order=%s)",
                    o.id, o.broker_order_id,
                )
                continue
            o.status = OrderStatus.CANCELED
            o.closed_at = now
            cancelled.append(o.id)
            published.append(o)
        if cancelled:
            db.commit()
            for o in published:
                db.refresh(o)
                events.publish(item.subscriber_user_id, _order_event("order.cancelled", o))
    return cancelled


def _working_entry_order_ids(item: "_PendingMirror") -> list[uuid.UUID]:
    """Read-only: the subscriber's still-WORKING, unfilled ENTRY orders for the
    SAME contract as this close (the OPPOSITE side — a BUY blocking a SELL close).
    Used to decide whether a close should be DEFERRED (entry still on its way)
    rather than rejected. Distinct from _cancel_subscriber_conflicts: this only
    LOOKS, it never cancels."""
    req = item.request
    entry_side = OrderSide.BUY if req.side == OrderSide.SELL else OrderSide.SELL
    with SessionLocal() as db:
        rows = db.execute(
            select(Order.id).where(
                Order.user_id == item.subscriber_user_id,
                Order.broker_account_id == item.broker_account_id,
                Order.instrument_type == req.instrument_type,
                Order.symbol == req.symbol,
                Order.option_expiry.is_not_distinct_from(req.option_expiry),
                Order.option_strike.is_not_distinct_from(req.option_strike),
                Order.option_right.is_not_distinct_from(req.option_right),
                Order.side == entry_side,
                Order.is_closing.is_(False),
                Order.status.in_(_WORKING_ORDER_STATUSES),
                func.coalesce(Order.filled_quantity, 0) < Order.quantity,
                Order.id != item.child_order_id,
            )
        ).scalars().all()
    return list(rows)


def _working_protective_stop_ids(
    item: "_PendingMirror", db: Session | None = None
) -> list[uuid.UUID]:
    """IDs of the subscriber's still-WORKING protective STOP orders (stop-loss)
    for the SAME contract as this mirror, excluding the order being placed.

    Used to avoid cancelling a stop-loss to place a conflicting take-profit: on
    Alpaca a position's shares back only ONE resting sell, so a trader resting
    BOTH collides — and the STOP (downside protection) must win. Read-only; opens
    its own session on the worker-thread call path (``db`` None) or uses the
    caller's session when passed (tests)."""
    req = item.request

    def _q(sess: Session) -> list[uuid.UUID]:
        return list(sess.execute(
            select(Order.id).where(
                Order.user_id == item.subscriber_user_id,
                Order.broker_account_id == item.broker_account_id,
                Order.instrument_type == req.instrument_type,
                Order.symbol == req.symbol,
                Order.option_expiry.is_not_distinct_from(req.option_expiry),
                Order.option_strike.is_not_distinct_from(req.option_strike),
                Order.option_right.is_not_distinct_from(req.option_right),
                Order.order_type.in_(_PROTECTIVE_STOP_TYPES),
                Order.status.in_(_WORKING_ORDER_STATUSES),
                Order.broker_order_id.isnot(None),
                Order.id != item.child_order_id,
            )
        ).scalars().all())

    if db is not None:
        return _q(db)
    with SessionLocal() as own:
        return _q(own)


def _has_working_entry_for_contract(db: Session, user_id: uuid.UUID, order: Order) -> bool:
    """True if the subscriber has a still-WORKING ENTRY (opposite side of this
    close) for the exact contract. Lets the fanout tell a FILL-SYNC RACE (entry
    filled at the broker but not yet reflected in our filled_quantity) apart from
    a genuine 'nothing to close' — so it doesn't shrink a close to zero and skip
    it while the entry's fill is still landing. Uses the caller's session."""
    entry_side = OrderSide.BUY if order.side == OrderSide.SELL else OrderSide.SELL
    return db.execute(
        select(Order.id).where(
            Order.user_id == user_id,
            Order.symbol == order.symbol,
            Order.instrument_type == order.instrument_type,
            Order.option_expiry.is_not_distinct_from(order.option_expiry),
            Order.option_strike.is_not_distinct_from(order.option_strike),
            Order.option_right.is_not_distinct_from(order.option_right),
            Order.side == entry_side,
            Order.is_closing.is_(False),
            Order.status.in_(_WORKING_ORDER_STATUSES),
        ).limit(1)
    ).first() is not None


def _has_filled_entry_for_contract(db: Session, user_id: uuid.UUID, order: Order) -> bool:
    """True if the subscriber has a same-contract ENTRY (opposite side of this
    close) whose STATUS is FILLED/PARTIALLY_FILLED — i.e. the broker HAS filled it
    — even if its ``filled_quantity`` hasn't synced into our numbers yet.

    This is the companion to [[_has_working_entry_for_contract]] for a subtler
    fill-sync race we hit in prod on Webull/SnapTrade: the entry reached FILLED in
    our DB but its filled_quantity was still 0 (SnapTrade's order feed reports the
    status ahead of the quantity), so ``_closeable_quantity`` read 0 and the close
    was wrongly skipped even though the broker held the position. A rejected entry
    (e.g. Alpaca can't trade an index option) is NOT filled, so this stays False
    and genuine 'never acquired' closes are still cleanly skipped."""
    entry_side = OrderSide.BUY if order.side == OrderSide.SELL else OrderSide.SELL
    return db.execute(
        select(Order.id).where(
            Order.user_id == user_id,
            Order.symbol == order.symbol,
            Order.instrument_type == order.instrument_type,
            Order.option_expiry.is_not_distinct_from(order.option_expiry),
            Order.option_strike.is_not_distinct_from(order.option_strike),
            Order.option_right.is_not_distinct_from(order.option_right),
            Order.side == entry_side,
            Order.is_closing.is_(False),
            Order.status.in_((OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)),
        ).limit(1)
    ).first() is not None


def _marketable_stock_limit(adapter: Any, req: BrokerOrderRequest) -> Decimal | None:
    """Price a marketable LIMIT for a stock — through the last trade so it fills
    like a market order (BUY a touch up, SELL a touch down). Returns None if no
    price is available."""
    if not hasattr(adapter, "get_stock_latest_price"):
        return None
    try:
        last = adapter.get_stock_latest_price(req.symbol)
    except Exception:  # noqa: BLE001
        last = None
    if not last or last <= 0:
        return None
    buf = Decimal("1.01") if req.side == OrderSide.BUY else Decimal("0.99")
    limit = (last * buf).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return limit if limit > 0 else None


def _alpaca_extended_hours(adapter: Any) -> bool:
    """True when this is an Alpaca adapter AND we're in pre/post-market right now
    — the case where a plain MARKET order can't fill and we must route an
    extended-hours LIMIT instead. Webull (via SnapTrade) trades extended hours
    natively, so it stays False there."""
    try:
        from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415
        from app.services import market_hours  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    return isinstance(adapter, AlpacaAdapter) and market_hours.in_extended_hours()


def _alpaca_regular_session(adapter: Any) -> bool:
    """True when this is an Alpaca adapter AND we're in the regular US session —
    the window where a plain option MARKET order is accepted and fills at the
    market (options trade RTH-only). Outside it we price a marketable limit."""
    try:
        from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415
        from app.services import market_hours  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    return isinstance(adapter, AlpacaAdapter) and market_hours.in_regular_session()


def _marketable_option_limit(adapter: Any, req: BrokerOrderRequest) -> BrokerOrderRequest:
    """Rewrite an option order as a MARKETABLE LIMIT priced through the book (SELL
    → bid, BUY → ask) so it fills immediately. Returns the order unchanged when no
    usable quote is available (no worse than not rewriting). Used for non-Alpaca
    brokers, and as the fallback when an Alpaca option MARKET order is refused for
    lack of a quotable NBBO."""
    if not (req.option_expiry and req.option_strike and req.option_right):
        return req
    if not hasattr(adapter, "get_option_latest_quote"):
        return req
    try:
        from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415
        occ = build_occ_symbol(
            req.symbol, req.option_expiry, req.option_strike, req.option_right.value
        )
        bid, ask = adapter.get_option_latest_quote(occ)
    except Exception:  # noqa: BLE001
        log.warning("immediate-close: option quote failed for %s — leaving order as-is", req.symbol)
        return req
    # SELL hits the bid, BUY (cover short) lifts the ask — either fills now.
    px = bid if req.side == OrderSide.SELL else ask
    if px is None or px <= 0:
        log.warning("immediate-close: no usable option quote for %s — leaving order as-is", req.symbol)
        return req
    from app.services.trader_bracket_monitor import _round_close_limit  # noqa: PLC0415
    limit = _round_close_limit(px, req.side)  # rounds to a valid, fill-friendly option tick
    return replace(req, order_type=OrderType.LIMIT, limit_price=limit, stop_price=None)


def _live_option_premium(db: Any, trader: Any, trader_order: Order) -> "Decimal | None":
    """Best-effort LIVE per-contract option premium for the max-per-contract gate,
    used only when the trader's OWN price isn't recorded yet — a MARKET option
    order fanned out in the ~ms window before its fill commits, so
    ``filled_avg_price`` is None and there's no ``limit_price``. Without a price
    the cap fails OPEN and lets over-cap options through (QA 2026-08: a $145/contract
    AAPL market option slipped a $100 cap). Priced off the TRADER's Alpaca account
    (the adapter that exposes option quotes). Returns None for a non-Alpaca trader
    or when no quote is available — the gate then keeps its prior fail-open."""
    if not (trader_order.option_expiry and trader_order.option_strike and trader_order.option_right):
        return None
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == trader.id,
            BrokerAccount.broker == BrokerName.ALPACA,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return None
    try:
        from app.brokers import adapter_for  # noqa: PLC0415
        from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol  # noqa: PLC0415
        from app.services.crypto import decrypt_json  # noqa: PLC0415
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
        if not isinstance(adapter, AlpacaAdapter):
            return None
        occ = build_occ_symbol(
            trader_order.symbol, trader_order.option_expiry,
            trader_order.option_strike, trader_order.option_right.value,
        )
        bid, ask = adapter.get_option_latest_quote(occ)
    except Exception:  # noqa: BLE001
        log.warning("max_per_contract: live option quote failed for %s", trader_order.symbol)
        return None
    # Per-contract COST: a BUY pays the ask, a SELL receives the bid; fall back to
    # whichever side is quoted so a one-sided book still yields a price.
    return (ask or bid) if trader_order.side == OrderSide.BUY else (bid or ask)


def _option_market_no_quote(msg: str) -> bool:
    """True when a broker refused an option MARKET order because it had no
    quotable price ("no available quote" / no NBBO) — retryable as a marketable
    LIMIT. Deliberately narrow: other 40310000 errors (insufficient qty, not
    fractionable, uncovered) are handled elsewhere and must NOT trigger this."""
    m = msg.lower()
    return "no available quote" in m or "no quote available" in m or "no nbbo" in m


def _ext_hours_limit_price(
    adapter: Any, req: BrokerOrderRequest, trader_ref_price: "Decimal | None"
) -> "Decimal | None":
    """Price an Alpaca extended-hours (pre/post-market) LIMIT. Prefer the TRADER's
    fill price as the anchor — a BUY bids it × (1 + cap), a SELL offers it × (1 −
    cap) — so the limit is marketable relative to where the trader actually traded,
    not our own last-trade quote, which can diverge wildly pre-market on thin names
    (prod STFS 2026-07-29: our quote ~3.09 vs the trader's 4.95 fill). The cap also
    bounds the chase. Falls back to the local marketable-limit when we have no
    trader anchor (e.g. the trader's order isn't filled yet). None if unpriceable."""
    if trader_ref_price is not None and trader_ref_price > 0:
        cap = Decimal(str(get_settings().mirror_ext_hours_slippage_pct)) / Decimal("100")
        buf = (Decimal("1") + cap) if req.side == OrderSide.BUY else (Decimal("1") - cap)
        px = (trader_ref_price * buf).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if px > 0:
            return px
    return _marketable_stock_limit(adapter, req)


def _to_immediate_close(
    adapter: Any, req: BrokerOrderRequest, trader_ref_price: "Decimal | None" = None
) -> BrokerOrderRequest:
    """Rewrite a CLOSE (or forced ENTRY) so it fills IMMEDIATELY — so a subscriber
    always exits/enters when the trader does. A copied LIMIT routinely rests
    unfilled (the price moves during copy latency), leaving the subscriber stuck.
    This forces it:

      * STOCK  → MARKET in regular hours. In extended hours Alpaca is LIMIT-only,
        so a marketable LIMIT anchored to ``trader_ref_price`` (the trader's fill)
        when known, else our last-trade quote — see _ext_hours_limit_price.
      * OPTION → MARKET on Alpaca during regular hours (Alpaca DOES accept option
        market orders in-session, and a market order fills like the trader's — no
        stale-quote risk). Outside regular hours, or on non-Alpaca brokers, a
        MARKETABLE LIMIT priced through the book (SELL → bid, BUY → ask). A rare
        "no available quote" rejection on the Alpaca market order falls back to
        the marketable limit in the place path (_option_market_no_quote).

    ``trader_ref_price`` is the trader's own fill price; passed only when the
    trader has filled, and used solely for the extended-hours stock anchor above.
    Works in either direction (BUY → ask / SELL → bid).
    """
    if req.instrument_type == InstrumentType.STOCK:
        # Pre/post-market on Alpaca a plain MARKET order can't fill — Alpaca only
        # trades extended hours as a LIMIT + extended_hours=True. This is exactly
        # the EHGO case: the trader (Webull) filled pre-market but the subscriber's
        # forced-MARKET mirror sat queued on Alpaca until 09:30, and a SELL on top
        # of that stuck BUY was wash-trade-rejected. Route a marketable extended-
        # hours limit so it fills now. Regular hours (and Webull, which trades
        # extended hours natively) keep MARKET.
        if _alpaca_extended_hours(adapter):
            px = _ext_hours_limit_price(adapter, req, trader_ref_price)
            if px is not None:
                return replace(
                    req, order_type=OrderType.LIMIT, limit_price=px,
                    stop_price=None, extended_hours=True,
                )
            # Couldn't price it — fall through to MARKET (no worse than before).
        return replace(req, order_type=OrderType.MARKET, limit_price=None, stop_price=None)

    # ── OPTION ──
    # Alpaca accepts option MARKET orders during regular hours, and a market order
    # fills like the trader's — no stale-quote risk (the marketable-limit route
    # could rest above a moved/wide quote and never fill: prod NVDA C195 SELL,
    # 2026-07-29, sat as a $0.55 limit and was cancelled unfilled). Prefer MARKET
    # there; a rare "no available quote" rejection falls back to the marketable
    # limit in the place path. Non-Alpaca / outside RTH keeps the marketable limit.
    if _alpaca_regular_session(adapter):
        return replace(req, order_type=OrderType.MARKET, limit_price=None, stop_price=None)
    return _marketable_option_limit(adapter, req)


def _market_type_refused(msg: str) -> bool:
    """True when a broker rejected a MARKET order because it won't take that
    ORDER TYPE right now (a trading halt, or an illiquid stock) — not the trade
    itself. The message says "please place a limit order instead"; a LIMIT is
    accepted and rests/fills when trading resumes."""
    m = msg.lower()
    return (
        "does not support market" in m
        or "limited liquidity" in m
        or "trading halt" in m
        or "place a limit order" in m
        or "halted" in m
    )


def _retry_stock_market_as_limit(item: "_PendingMirror", req: BrokerOrderRequest) -> BrokerOrderResult | None:
    """A copied STOCK MARKET order was refused (halt / illiquid). Retry ONCE as a
    marketable LIMIT priced just through the last trade (BUY a touch up, SELL a
    touch down) so it fills the instant trading resumes. Returns the result on
    success, or None if we can't price it (caller then re-raises the original)."""
    if req.instrument_type != InstrumentType.STOCK or req.order_type != OrderType.MARKET:
        return None
    if not hasattr(item.adapter, "get_stock_latest_price"):
        return None
    try:
        last = item.adapter.get_stock_latest_price(req.symbol)
    except Exception:  # noqa: BLE001
        last = None
    if not last or last <= 0:
        return None
    buf = Decimal("1.01") if req.side == OrderSide.BUY else Decimal("0.99")
    limit = (last * buf).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if limit <= 0:
        return None
    retry = replace(req, order_type=OrderType.LIMIT, limit_price=limit, stop_price=None)
    item.request = retry
    log.info("mirror %s market refused (halt/illiquid) — retrying as LIMIT %s", req.symbol, limit)
    return item.adapter.place_order(retry)


def _place_mirror_with_conflict_resolve(item: "_PendingMirror") -> BrokerOrderResult:
    """Place the mirror order. If a CLOSE is rejected because another working
    order on the subscriber's account blocks it (wash trade / uncovered /
    insufficient qty), cancel those orders and retry — the copy-engine analog of
    the direct-close auto-resolve in api.trades. Runs in a worker thread (called
    via ``asyncio.to_thread``); raising propagates to the normal reject path."""
    req = item.request
    # We only FORCE a close to fill immediately (market / marketable-limit, and
    # cancel any dangling / leftover entry) once the TRADER's OWN close has
    # actually FILLED — i.e. the trader has genuinely exited. While the trader's
    # close is still WORKING we mirror their LIMIT unchanged, so the subscriber
    # rests a cancellable order at a potentially better price; if the trader then
    # CANCELS, the cancel propagates and the mirror is cancelled instead of
    # leaving the subscriber with a phantom fill (the 3.20 divergence). Part B —
    # force_fill_mirrors_to_market, fired from the listeners when a trader's
    # working close LATER fills — sweeps any still-resting mirror to market so
    # the exit is still guaranteed. Entries are never touched either way.
    if item.trader_filled:
        # The fanout already flags most closes from the subscriber's DB position,
        # but that can LAG — the entry fill may not have synced yet. And we CANNOT
        # trust the broker's is_closing flag: SnapTrade reports Webull actions as
        # plain BUY/SELL, so a genuine close has is_closing=False for OPTIONS as
        # well as stocks. So for any SELL our DB did NOT flag as closing, ask the
        # BROKER directly: if the subscriber actually holds the position this SELL
        # would reduce, it's really a close. (We skip BUYs to avoid a
        # get_positions call on every entry.)
        should_close_now = req.is_closing
        cancelled_working_entry = False
        if req.side == OrderSide.SELL and req.instrument_type in (
            InstrumentType.STOCK, InstrumentType.OPTION
        ):
            # Ask the broker what the subscriber actually holds. This drives BOTH
            # close-detection (SnapTrade never sets is_closing, so a genuine close
            # arrives flagged False) AND the defer/dangling decision below — so we
            # run it for flagged closes too, not just unflagged ones.
            held = live_closeable_quantity(item.adapter, req)
            if held is not None and held > 0:
                should_close_now = True                        # position held → close it
            elif held is not None and held == 0:
                # Nothing held yet. Is this SELL a CLOSE — flagged, or does the
                # subscriber have a still-working ENTRY it would close?
                entry_ids = _working_entry_order_ids(item)
                if should_close_now or entry_ids:
                    if entry_ids:
                        # The entry is on its way (pre-market BUY queued for the
                        # 09:30 open, or a fast scalp where the SELL beat the BUY).
                        # Placing the SELL now just rejects (Alpaca "opposite side
                        # order exists" / SnapTrade "no position"). Give a just-
                        # landed fill a brief beat; if still nothing, DEFER — park
                        # the close and fire it the instant the entry fills
                        # (fire_deferred_closes_for_entry). We do NOT cancel the entry.
                        filled_during_recheck = False
                        for _ in range(3):
                            recheck = live_closeable_quantity(item.adapter, req)
                            if recheck is not None and recheck > 0:
                                should_close_now = True  # fill landed → close now
                                filled_during_recheck = True
                                break
                            time.sleep(1.0)
                        if not filled_during_recheck:
                            raise _DeferUntilEntryFills(entry_ids)
                    else:
                        # Flagged close, no position, and no entry coming → nothing
                        # to sell. Cancel any leftover working order; report the
                        # mirror skipped. Never place a naked SELL.
                        cancelled = _cancel_subscriber_conflicts(item)
                        raise _DanglingEntryCancelled(cancelled)
                elif not get_settings().copy_allow_opening_shorts:
                    # NOT flagged as a close and no working entry, but the
                    # subscriber holds nothing — placing this SELL would open a
                    # NAKED SHORT. SnapTrade rejects it; Alpaca actually shorts
                    # (prod HUIZ). Since Webull orders always arrive is_closing=
                    # False, a genuine close of a MISSED entry is indistinguishable
                    # from a deliberate short — so default to skipping rather than
                    # shorting the subscriber. Opt in via copy_allow_opening_shorts.
                    raise _OpeningShortSkipped()
                # else (copy_allow_opening_shorts=true): deliberate opening short —
                # fall through and place it.
        if should_close_now:
            req = _to_immediate_close(item.adapter, req, item.trader_fill_price)
            if not req.is_closing:
                # Mark closing so the conflict-resolve + live re-clamp below
                # treat it as a close too.
                req = replace(req, is_closing=True)
            item.request = req
            # Flatten FULLY. The fanout already clamps the close to what the
            # subscriber holds (e.g. trader sold 10 but only 6 filled → sell 6).
            # But the other 4 may still be a WORKING, partially-filled entry — if
            # it fills later the subscriber is stranded long in a name the trader
            # has left. The trader has now exited, so their accumulation window is
            # over: cancel any leftover same-contract working entry before placing
            # the close. (Skip if the race path above already cancelled it.)
            if not cancelled_working_entry:
                _cancel_subscriber_conflicts(item)
        elif req.instrument_type in (InstrumentType.STOCK, InstrumentType.OPTION):
            # NOT a close, but the trader has FILLED — so this is an ENTRY the
            # trader just got into. Force the subscriber's entry to fill at market
            # too, so they actually get INTO the trade (their copied limit may not
            # reach on the subscriber's venue — the "trader filled, subscriber
            # didn't" gap). _to_immediate_close prices through the market in the
            # order's own direction (BUY → ask), so it works for entries as well.
            req = _to_immediate_close(item.adapter, req, item.trader_fill_price)
            item.request = req
    # Non-forced entry pre/post-market on Alpaca: a plain limit won't trade in
    # extended hours unless flagged, so a resting pre-market mirror would sit
    # unfilled until 09:30 (the other half of the EHGO problem). Flag it so it
    # can fill now, matching the trader (who trades extended hours). Stocks only;
    # skip if already flagged or a bracket is attached (Alpaca forbids
    # extended_hours with brackets).
    if (
        req.instrument_type == InstrumentType.STOCK
        and req.order_type == OrderType.LIMIT
        and not req.extended_hours
        and req.take_profit_price is None
        and req.stop_loss_price is None
        and _alpaca_extended_hours(item.adapter)
    ):
        req = replace(req, extended_hours=True)
        item.request = req
    try:
        return item.adapter.place_order(req)
    except Exception as exc:  # noqa: BLE001
        # Non-fractionable asset + fractional mirror qty (a fractional multiplier
        # can produce e.g. 2.5 shares of a stock Alpaca won't split) → round the
        # qty DOWN to whole and retry. item.request is updated so Phase 3 records
        # the quantity actually placed.
        if "not fractionable" in str(exc).lower():
            whole = req.quantity.to_integral_value(rounding=ROUND_DOWN)
            if whole > 0 and whole != req.quantity:
                req = replace(req, quantity=whole)
                item.request = req
                try:
                    return item.adapter.place_order(req)
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2  # fall through to conflict handling with new error
        # Broker refused the MARKET order TYPE — a trading halt or an illiquid
        # stock ("please place a limit order instead"), not the trade. Applies to
        # BOTH a forced entry and a forced close, so handle it before the
        # close-only conflict logic. Retry once as a marketable limit.
        if _market_type_refused(str(exc)):
            try:
                retried = _retry_stock_market_as_limit(item, req)
            except Exception as exc2:  # noqa: BLE001
                raise exc2 from exc
            if retried is not None:
                return retried
        # Alpaca refused an option MARKET order for lack of a quotable NBBO ("no
        # available quote") — a rare quote gap or the near-close options cutoff.
        # Retry as a marketable LIMIT priced through the book. Applies to a forced
        # entry and a forced close alike (both route through _to_immediate_close).
        if (
            req.instrument_type == InstrumentType.OPTION
            and req.order_type == OrderType.MARKET
            and _option_market_no_quote(str(exc))
        ):
            limit_req = _marketable_option_limit(item.adapter, req)
            if limit_req.order_type == OrderType.LIMIT:
                item.request = limit_req
                try:
                    return item.adapter.place_order(limit_req)
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2  # fall through to the remaining handling with the new error
        if not (req.is_closing and is_order_conflict_error(exc)):
            raise

        # Guard: never cancel a protective STOP to place a conflicting take-profit.
        # A trader who rests BOTH a stop-loss and a take-profit on the same position
        # (a manual OCO) collides on Alpaca, where a position's shares back only ONE
        # resting sell. The conflict-resolve below would cancel the blocker — and
        # that blocker is the STOP, silently removing downside protection (prod STKH
        # 2026-07-28). So when THIS mirror is a non-forced resting LIMIT and a working
        # stop-loss already guards the position, KEEP the stop and skip this order;
        # the subscriber still exits when the trader's take-profit fills. A genuine
        # exit (trader_filled → forced market/marketable close) is unaffected, and a
        # stop-vs-stop modify (req is itself a STOP) also proceeds normally below.
        if not item.trader_filled and req.order_type == OrderType.LIMIT:
            kept = _working_protective_stop_ids(item)
            if kept:
                raise _KeptProtectiveStop(kept)

        # Re-clamp to the broker's LIVE held quantity (source of truth). Fixes
        # the fill-sync-lag case where our DB thought the subscriber held more
        # than they do — e.g. a mirror close that only PARTIALLY filled, so the
        # next close for the full size is rejected as "in excess of current
        # holding". Only ever SHRINKS the order, so it can never oversell.
        live = live_closeable_quantity(item.adapter, req)
        reclamped = False
        if live is not None:
            if live <= 0:
                # Broker says the subscriber is already flat — nothing to close.
                raise RuntimeError(
                    f"position_already_flat: broker reports 0 held for {req.symbol}"
                )
            if live < req.quantity:
                log.info(
                    "mirror close re-clamped to live held qty %s for %s (was %s)",
                    live, req.symbol, req.quantity,
                )
                req = replace(req, quantity=live)
                item.request = req
                reclamped = True

        # Also cancel any of our OWN working orders reserving the position.
        cancelled = _cancel_subscriber_conflicts(item)
        if not reclamped and not cancelled:
            raise  # neither an oversized qty nor a cancellable order — retry won't help

        last_exc: BaseException = exc
        for _ in range(3):
            time.sleep(0.5)  # let the broker release the reservation
            try:
                return item.adapter.place_order(req)
            except Exception as exc2:  # noqa: BLE001
                last_exc = exc2
                if is_order_conflict_error(exc2):
                    # Held qty may have moved again — re-clamp once more.
                    live2 = live_closeable_quantity(item.adapter, req)
                    if live2 is not None and 0 < live2 < req.quantity:
                        req = replace(req, quantity=live2)
                        item.request = req
                    continue
                raise
        raise last_exc


def _closeable_quantity(
    db: Session, user_id: uuid.UUID, order: Order, subtract_reserved: bool = True,
    exclude_order_id: uuid.UUID | None = None,
) -> Decimal:
    """Quantity the subscriber can still CLOSE in ``order``'s direction: their
    net filled position for the contract, MINUS what their own still-working
    orders on the same side have already reserved at the broker.

    ``exclude_order_id`` drops one order from the net — used when asking "does
    THIS order close a held position?" about the caller's OWN order: without
    excluding it, a FULL close (the order that takes the position to flat) counts
    its own fill in the net and reads 0, i.e. "not closing". That's how a paused
    trader's market close of a whole position was mis-read as an open and dropped
    from fanout, stranding subscribers long (QA 2026-08). Excluding the order
    itself yields the position that existed BEFORE it.

    Two things reduce what a new close can take:
      * net filled position (filled buys − sells) — what they actually hold;
      * unfilled qty on open same-side orders — the broker's ``held_for_orders``.
        Without subtracting this, a second close of shares a prior working close
        already reserved rejects with "insufficient qty available".

    ``subtract_reserved=False`` skips the second term — clamp to the raw held
    position only. Used by the fanout close-clamp, which pairs with the
    copy-engine conflict-resolve retry: we place the full held quantity and, if a
    working order reserves it, CANCEL that order and retry rather than shrinking
    the close around it.

    Tracks reality as fills sync in (SnapTrade reconciler + fills_sync). Returns
    a non-negative quantity."""
    same_contract = [
        Order.user_id == user_id,
        Order.symbol == order.symbol,
        Order.instrument_type == order.instrument_type,
        Order.option_expiry.is_not_distinct_from(order.option_expiry),
        Order.option_strike.is_not_distinct_from(order.option_strike),
        Order.option_right.is_not_distinct_from(order.option_right),
    ]
    if exclude_order_id is not None:
        same_contract.append(Order.id != exclude_order_id)
    same_contract = tuple(same_contract)
    # Net filled position (signed long).
    rows = db.execute(
        select(Order.side, func.coalesce(func.sum(Order.filled_quantity), 0))
        .where(*same_contract)
        .group_by(Order.side)
    ).all()
    buys = Decimal(0)
    sells = Decimal(0)
    for side, qty in rows:
        if side == OrderSide.BUY:
            buys = Decimal(str(qty))
        elif side == OrderSide.SELL:
            sells = Decimal(str(qty))
    net_long = buys - sells
    net_in_direction = net_long if order.side == OrderSide.SELL else -net_long

    # Unfilled qty already reserved by this subscriber's OWN working orders on
    # the same side (buy-to-close for a short, sell-to-close for a long).
    reserved = Decimal(0)
    if subtract_reserved:
        reserved = Decimal(str(db.execute(
            select(
                func.coalesce(
                    func.sum(Order.quantity - func.coalesce(Order.filled_quantity, 0)), 0
                )
            ).where(
                *same_contract,
                Order.side == order.side,
                Order.status.in_(_WORKING_ORDER_STATUSES),
            )
        ).scalar_one()))

    closeable = net_in_direction - reserved
    return closeable if closeable > 0 else Decimal(0)


# Statuses a mirror can be modified in: fully working AND untouched by any
# fill. PARTIALLY_FILLED is deliberately excluded — cancel+replace of the full
# new quantity would double-count the portion that already filled.
_MODIFIABLE_MIRROR_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
)


def propagate_modify_to_mirrors(trader_order_id: uuid.UUID) -> None:
    """A trader modified their still-working order (new limit / stop / qty /
    type) at the broker. Propagate that to every still-working, unfilled
    subscriber mirror via cancel-and-replace: cancel the old broker order, place
    a replacement with the re-scaled terms, and update the mirror row in place.

    Broker adapters expose no native "replace", so cancel+replace is the only
    broker-agnostic path (Alpaca / SnapTrade / IBKR all support cancel + place).
    Modelled on ``trades._run_cancel_fanout_in_background``: runs in a worker/
    background thread with a small pool for the blocking SDK calls, and per-
    mirror failures are audited, never raised.

    Mirrors that are partially/fully filled or terminal are skipped — they
    can't be safely modified by cancel+replace."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with SessionLocal() as db:
        trader_order = db.get(Order, trader_order_id)
        if trader_order is None:
            return
        children = list(db.execute(
            select(Order).where(
                Order.parent_order_id == trader_order_id,
                Order.status.in_(_MODIFIABLE_MIRROR_STATUSES),
                func.coalesce(Order.filled_quantity, 0) == 0,
            )
        ).scalars())
        if not children:
            return

        pending: list[tuple[Order, Any, BrokerOrderRequest]] = []
        for child in children:
            if not child.broker_order_id:
                continue  # never reached the broker — nothing to replace
            acct = db.get(BrokerAccount, child.broker_account_id)
            if acct is None:
                continue

            # Re-scale off the trader's NEW quantity with the subscriber's
            # current multiplier, then apply the same close-clamp the original
            # fanout used so a modify can't oversell what they actually hold.
            sub = db.get(SubscriberSettings, child.user_id)
            multiplier = sub.multiplier if sub is not None else Decimal("1.000")
            new_qty = _scale_quantity(
                trader_order.quantity, multiplier, acct.supports_fractional
            )
            if trader_order.is_closing and new_qty > 0:
                # Clamp to the RAW held position (subtract_reserved=False). The
                # order being modified is itself a working close that RESERVES the
                # position; subtracting that reservation would compute closeable=0
                # and drop the modify entirely (held 5 − reserved-by-old-close 5 =
                # 0). We're about to CANCEL that old order, so it must not count.
                closeable = _closeable_quantity(
                    db, child.user_id, trader_order, subtract_reserved=False
                )
                if closeable < new_qty:
                    new_qty = closeable

            new_type = trader_order.order_type
            new_limit = trader_order.limit_price
            new_stop = trader_order.stop_price

            # No-op if nothing the subscriber's broker cares about changed, or
            # the clamp wiped the quantity to zero (leave the resting order be).
            if (
                child.quantity == new_qty
                and child.order_type == new_type
                and child.limit_price == new_limit
                and child.stop_price == new_stop
            ) or new_qty <= 0:
                continue

            try:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=child.user_id,
                    action="order.mirror_modify_creds_error",
                    entity_type="order", entity_id=child.id,
                    metadata={"parent_order_id": str(trader_order_id), "error": str(exc)[:300]},
                )
                continue

            pending.append((child, adapter, BrokerOrderRequest(
                instrument_type=child.instrument_type,
                symbol=child.symbol,
                side=child.side,
                order_type=new_type,
                quantity=new_qty,
                limit_price=new_limit,
                stop_price=new_stop,
                take_profit_price=None,
                stop_loss_price=None,
                option_expiry=child.option_expiry,
                option_strike=child.option_strike,
                option_right=child.option_right,
                is_closing=child.is_closing,
                client_order_id=str(child.id),
            )))

        if not pending:
            return

        def _replace(item: tuple[Order, Any, BrokerOrderRequest]):
            ch, ad, rq = item
            # Cancel the old resting order, THEN place the replacement. A cancel
            # failure almost always means the mirror just filled — abort the
            # replace so we never stack a duplicate order on top of a fill.
            try:
                ad.cancel_order(ch.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                return ch.id, None, f"cancel_failed: {exc}"[:300]
            try:
                return ch.id, ad.place_order(rq), None
            except Exception as exc:  # noqa: BLE001
                return ch.id, None, f"replace_failed: {exc}"[:300]

        with ThreadPoolExecutor(max_workers=min(32, len(pending))) as pool:
            results = list(pool.map(lambda it: _throttled_item(_replace, it), pending))

        req_by_id = {ch.id: rq for ch, _ad, rq in pending}
        for child_id, resp, err in results:
            ch = db.get(Order, child_id)
            if ch is None:
                continue
            if resp is not None:
                rq = req_by_id[child_id]
                ch.order_type = rq.order_type
                ch.quantity = rq.quantity
                ch.limit_price = rq.limit_price
                ch.stop_price = rq.stop_price
                ch.broker_order_id = resp.broker_order_id
                ch.status = resp.status
                ch.submitted_at = resp.submitted_at
                ch.filled_quantity = resp.filled_quantity
                ch.filled_avg_price = resp.filled_avg_price
                ch.closed_at = None
                ch.redis_published_at = datetime.now(timezone.utc)
                audit.record(
                    db, actor_user_id=ch.user_id, action="order.mirror_modified",
                    entity_type="order", entity_id=ch.id,
                    metadata={
                        "parent_order_id": str(trader_order_id),
                        "broker_order_id": resp.broker_order_id,
                        "order_type": rq.order_type.value,
                        "quantity": str(rq.quantity),
                        "limit_price": str(rq.limit_price) if rq.limit_price is not None else None,
                        "stop_price": str(rq.stop_price) if rq.stop_price is not None else None,
                    },
                )
                events.publish(ch.user_id, _order_event("order.copy_submitted", ch))
            else:
                # Cancel failed → OLD order is still live (subscriber keeps a
                # working order, just with stale terms) — leave status alone.
                # Replace failed AFTER a successful cancel → the mirror is now
                # gone; mark it canceled so our state is truthful.
                lost = err is not None and err.startswith("replace_failed")
                if lost:
                    ch.status = OrderStatus.CANCELED
                    ch.closed_at = datetime.now(timezone.utc)
                    events.publish(ch.user_id, _order_event("order.cancelled", ch))
                audit.record(
                    db, actor_user_id=ch.user_id, action="order.mirror_modify_failed",
                    entity_type="order", entity_id=ch.id,
                    metadata={
                        "parent_order_id": str(trader_order_id),
                        "broker_order_id": ch.broker_order_id,
                        "error": err,
                        "old_order_lost": lost,
                    },
                )
        db.commit()


# Re-place retry budget for the cancel+place FALLBACK (brokers without an atomic
# in-place replace). After cancelling the old order the broker can lag in
# releasing the shares it reserved, rejecting the immediate re-place with
# "insufficient qty" — so we wait briefly and retry. ~4 × 0.6s ≈ up to 1.8s.
_MODIFY_PLACE_ATTEMPTS = 4
_MODIFY_PLACE_BACKOFF_S = 0.6


def _modify_place_one(item: "tuple[Order, Any, BrokerOrderRequest, uuid.UUID]"):
    """Re-price ONE subscriber mirror (worker-thread step of the modify fanout).

    Prefers an ATOMIC in-place replace where the broker supports it (Alpaca): it
    changes price/qty without releasing the position's share reservation, so a
    rapid re-price can't race the release — the cancel+replace bug that lost
    subscriber sells on STKH (2026-07-28), where the just-cancelled order still
    showed the shares as held_for_orders and the immediate re-place was rejected.
    Atomic contract: on FAILURE the original order is left working untouched, so
    Phase 3 keeps the old mirror rather than strand the subscriber.

    Falls back to cancel+place for brokers with no in-place replace (SnapTrade /
    IBKR), retrying the PLACE briefly if the broker hasn't released the shares yet
    ("insufficient qty" / held_for_orders); without the retry the re-place is lost
    and the subscriber has no order mid-re-price.

    Returns ``(old_id, new_id, BrokerOrderResult | None, err_sentinel | None)``.
    Pure over its args (no DB), so it's safe in the thread pool and unit-testable.
    """
    old_ch, ad, rq, new_id = item
    if getattr(ad, "supports_replace", False):
        # Alpaca models a modify as a replacement CHAIN; a re-replace fired before
        # the prior replacement has settled fails TRANSIENTLY with 42210000 "order
        # chain not fully replaced" (prod RDGT 2026-08-10: a close re-priced 3s
        # after the previous re-price hit this and, with no retry, left the
        # subscriber holding a STALE-priced sell that never filled while the trader
        # had already exited). The chain settles in ~1-2s — retry the SAME replace
        # briefly. On ultimate failure we still return replace_failed, so the
        # atomic contract holds (original order untouched → Phase 3 keeps it).
        last_exc: BaseException | None = None
        for attempt in range(_MODIFY_PLACE_ATTEMPTS):
            try:
                return old_ch.id, new_id, ad.replace_order(old_ch.broker_order_id, rq), None
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if is_replace_chain_pending_error(exc) and attempt < _MODIFY_PLACE_ATTEMPTS - 1:
                    time.sleep(_MODIFY_PLACE_BACKOFF_S)
                    continue
                break
        return old_ch.id, new_id, None, f"replace_failed: {last_exc}"[:300]
    try:
        cancelled = ad.cancel_order(old_ch.broker_order_id)
    except Exception as exc:  # noqa: BLE001
        return old_ch.id, new_id, None, f"cancel_failed: {exc}"[:300]
    # A False here means the broker had nothing to cancel — the order is already
    # terminal, and the overwhelmingly likely reason is that it FILLED. Placing the
    # replacement now would double the position (prod doubled a META entry exactly
    # this way — SnapTrade's cancel returned 1070 while its feed still showed the
    # mirror working), so bail. The cancel result is the only signal reflecting the
    # broker's ACTUAL state at this moment.
    if cancelled is False:
        return old_ch.id, new_id, None, "cancel_noop_already_terminal"
    # Only a conflict error (share-release lag) is worth waiting on; anything else
    # (e.g. a bad price) will just fail again, so break out immediately.
    last_exc: BaseException | None = None
    for attempt in range(_MODIFY_PLACE_ATTEMPTS):
        try:
            return old_ch.id, new_id, ad.place_order(rq), None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if is_order_conflict_error(exc) and attempt < _MODIFY_PLACE_ATTEMPTS - 1:
                time.sleep(_MODIFY_PLACE_BACKOFF_S)
                continue
            break
    return old_ch.id, new_id, None, f"place_failed: {last_exc}"[:300]


def cancel_and_replace_mirrors_for_modify(
    old_trader_order_id: uuid.UUID, new_trader_order_id: uuid.UUID
) -> None:
    """Trader modified a working order, represented app-wide as cancel-old +
    place-new. For every still-working, UNFILLED subscriber mirror of the OLD
    trader order: cancel it at the subscriber's broker, mark that mirror
    CANCELED, and place a brand-NEW mirror order (a fresh row) linked to the NEW
    trader order with the re-scaled modified terms.

    Differs from ``propagate_modify_to_mirrors`` (which updates the mirror row
    in place): here the old mirror stays in history as CANCELED and the new
    order is a separate row — matching the trader-side cancel+new representation.

    Only touches fully-working, unfilled mirrors: a partially/fully filled
    mirror is a real (partial) position, and placing a new order on top would
    double the subscriber's exposure. Per-mirror failures are audited, never
    raised. A cancel failure aborts that mirror's replace (the old order is
    likely mid-fill) so we never stack a new order on a fill."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with SessionLocal() as db:
        new_order = db.get(Order, new_trader_order_id)
        if new_order is None:
            return
        children = list(db.execute(
            select(Order).where(
                Order.parent_order_id == old_trader_order_id,
                Order.status.in_(_MODIFIABLE_MIRROR_STATUSES),
                func.coalesce(Order.filled_quantity, 0) == 0,
            )
        ).scalars())
        if not children:
            return

        # Phase 1 (session thread): build the plan. Pre-generate the NEW mirror
        # id so it can be the broker client_order_id, but only INSERT the row in
        # phase 3 on success — a cancel failure leaves no phantom row behind.
        plan: list[tuple[Order, Any, BrokerOrderRequest, uuid.UUID]] = []
        for child in children:
            if not child.broker_order_id:
                continue
            acct = db.get(BrokerAccount, child.broker_account_id)
            if acct is None:
                continue
            sub = db.get(SubscriberSettings, child.user_id)
            multiplier = sub.multiplier if sub is not None else Decimal("1.000")
            new_qty = _scale_quantity(
                new_order.quantity, multiplier, acct.supports_fractional
            )
            if new_order.is_closing and new_qty > 0:
                # Clamp to the RAW held position (subtract_reserved=False). The
                # old mirror we're about to CANCEL is a working close that
                # RESERVES the position; subtracting it would compute closeable=0
                # and skip the whole modify (the prod bug: an AMZN close-price
                # change from 3.00→3.20 never reached subscribers because their
                # own resting close reserved the shares).
                closeable = _closeable_quantity(
                    db, child.user_id, new_order, subtract_reserved=False
                )
                if closeable < new_qty:
                    new_qty = closeable
            if new_qty <= 0:
                continue
            try:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=child.user_id,
                    action="order.mirror_modify_creds_error",
                    entity_type="order", entity_id=child.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "error": str(exc)[:300]},
                )
                continue
            new_child_id = uuid.uuid4()
            plan.append((child, adapter, BrokerOrderRequest(
                instrument_type=child.instrument_type,
                symbol=child.symbol,
                side=child.side,
                order_type=new_order.order_type,
                quantity=new_qty,
                limit_price=new_order.limit_price,
                stop_price=new_order.stop_price,
                take_profit_price=None,
                stop_loss_price=None,
                option_expiry=child.option_expiry,
                option_strike=child.option_strike,
                option_right=child.option_right,
                is_closing=child.is_closing,
                client_order_id=str(new_child_id),
            ), new_child_id))

        if not plan:
            return

        # Phase 2 (thread pool): re-price each mirror — atomic in-place replace
        # where supported, else cancel+place with a share-release retry (see
        # _modify_place_one).
        with ThreadPoolExecutor(max_workers=min(32, len(plan))) as pool:
            results = list(pool.map(lambda it: _throttled_item(_modify_place_one, it), plan))

        # Phase 3 (session thread): apply.
        req_by_new_id = {new_id: rq for _c, _a, rq, new_id in plan}
        for old_id, new_id, resp, err in results:
            old_ch = db.get(Order, old_id)
            if old_ch is None:
                continue
            rq = req_by_new_id[new_id]
            if err == "cancel_noop_already_terminal":
                # The broker had nothing to cancel — the mirror is already
                # terminal, and that almost always means it FILLED. Place
                # nothing (a replacement doubles the position) and do NOT mark
                # it CANCELED, because it isn't. Leave the row alone; fills_sync
                # settles it to FILLED on its next tick.
                audit.record(
                    db, actor_user_id=old_ch.user_id,
                    action="order.mirror_modify_skipped_already_terminal",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "broker_order_id": old_ch.broker_order_id},
                )
                continue
            if err is not None and err.startswith("cancel_failed"):
                # Old order still live (likely mid-fill) — leave it, place nothing.
                audit.record(
                    db, actor_user_id=old_ch.user_id, action="order.mirror_modify_cancel_failed",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "broker_order_id": old_ch.broker_order_id, "error": err},
                )
                continue
            if err is not None and err.startswith("replace_failed"):
                # Atomic in-place replace failed → the ORIGINAL order is untouched
                # and still working at its OLD terms. Do NOT mark it canceled: the
                # subscriber keeps a live order (just at the stale price), which is
                # strictly better than losing it. fills_sync / the next modify
                # settles it.
                audit.record(
                    db, actor_user_id=old_ch.user_id, action="order.mirror_replace_failed_kept_old",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "broker_order_id": old_ch.broker_order_id, "error": err},
                )
                continue
            # Cancel succeeded (or atomic replace succeeded) — the old mirror is
            # gone at the broker (replaced/canceled); a NEW row carries the result.
            old_ch.status = OrderStatus.CANCELED
            old_ch.closed_at = datetime.now(timezone.utc)
            events.publish(old_ch.user_id, _order_event("order.cancelled", old_ch))
            if resp is None:
                # Replace failed after a successful cancel — subscriber lost the
                # order. Truthfully leave the old mirror canceled; no new row.
                audit.record(
                    db, actor_user_id=old_ch.user_id, action="order.mirror_modify_failed",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "error": err, "old_order_lost": True},
                )
                continue
            # Place succeeded — insert the NEW mirror row linked to the NEW
            # trader order, carrying the broker's result.
            new_child = Order(
                id=new_id,
                user_id=old_ch.user_id,
                broker_account_id=old_ch.broker_account_id,
                parent_order_id=new_trader_order_id,
                instrument_type=old_ch.instrument_type,
                symbol=old_ch.symbol,
                option_expiry=old_ch.option_expiry,
                option_strike=old_ch.option_strike,
                option_right=old_ch.option_right,
                is_closing=old_ch.is_closing,
                side=old_ch.side,
                order_type=rq.order_type,
                quantity=rq.quantity,
                limit_price=rq.limit_price,
                stop_price=rq.stop_price,
                take_profit_price=None,
                stop_loss_price=None,
                status=resp.status,
                broker_order_id=resp.broker_order_id,
                filled_quantity=resp.filled_quantity,
                filled_avg_price=resp.filled_avg_price,
                submitted_at=resp.submitted_at,
                broker_accepted_at=resp.submitted_at or datetime.now(timezone.utc),
                redis_published_at=datetime.now(timezone.utc),
            )
            db.add(new_child)
            audit.record(
                db, actor_user_id=new_child.user_id, action="order.mirror_replaced_on_modify",
                entity_type="order", entity_id=new_child.id,
                metadata={
                    "old_mirror_id": str(old_ch.id),
                    "old_parent_order_id": str(old_trader_order_id),
                    "new_parent_order_id": str(new_trader_order_id),
                    "broker_order_id": resp.broker_order_id,
                    "quantity": str(rq.quantity),
                    "limit_price": str(rq.limit_price) if rq.limit_price is not None else None,
                },
            )
            db.flush()
            events.publish(new_child.user_id, _order_event("order.copy_submitted", new_child))
        db.commit()


# After a cancel is ACCEPTED, wait this long before re-reading the order's final
# fill. Alpaca's cancel is async and a marketable-limit mirror fills within ~ms,
# so the fill can land in the cancel window; the settle lets the broker record the
# TRUE final filled qty before we decide how much (if any) to force-fill.
_FORCE_FILL_SETTLE_S = 0.6


def _force_fill_cancel_then_place(item: "tuple[Order, Any, BrokerOrderRequest, uuid.UUID]"):
    """Cancel ONE resting mirror then place its forced market/marketable close
    (worker-thread step of force_fill_mirrors_to_market). Extracted to module
    level so it's pure over its args (no DB) and unit-testable — mirrors the
    cancel+place fallback of _modify_place_one.

    Returns ``(old_id, new_id, BrokerOrderResult | None, err_sentinel | None,
    meta | None)`` where meta carries ``{"already": Decimal, "rq": request}`` so
    the apply phase can record the partial fill and size the child correctly.
    """
    old_ch, ad, rq, new_id = item
    try:
        cancelled = ad.cancel_order(old_ch.broker_order_id)
    except Exception as exc:  # noqa: BLE001
        return old_ch.id, new_id, None, f"cancel_failed: {exc}"[:300], None
    # A False here means the broker had nothing to cancel — the order is already
    # terminal, and the overwhelmingly likely reason is that it FILLED. Placing
    # the replacement now would double the position, so bail. This is not
    # hypothetical: prod doubled a subscriber's META entry exactly this way.
    # SnapTrade returns 1070 ("failed to cancel") while its own order feed still
    # shows the mirror as working — its data lagged the real fill by ~42s — so
    # neither our DB nor a get_order re-check could see the truth. The cancel
    # result is the only signal that reflects the broker's ACTUAL state now.
    if cancelled is False:
        return old_ch.id, new_id, None, "cancel_noop_already_terminal", None
    # cancel==True means "cancel ACCEPTED", NOT "zero filled". Alpaca's cancel is
    # async and a marketable-limit mirror can FILL in that window — so a full-size
    # force-fill on top DOUBLES the subscriber (prod MSFT 2026-08: the "cancelled"
    # limit filled 4-5 while a full 5 went on top → sub held ~2×). Re-read the
    # order's FINAL fill after a short settle and place only the TRUE remainder
    # (full mirror qty − whatever actually filled); place NOTHING if it fully
    # filled. This is the double-buy guard.
    time.sleep(_FORCE_FILL_SETTLE_S)
    already = Decimal(str(old_ch.filled_quantity or 0))
    try:
        fin = ad.get_order(old_ch.broker_order_id)
        if fin is not None and fin.filled_quantity is not None:
            already = max(already, Decimal(str(fin.filled_quantity)))
    except Exception:  # noqa: BLE001
        pass
    true_remaining = Decimal(str(old_ch.quantity)) - already
    if true_remaining <= 0:
        # The "cancelled" order actually filled the whole mirror — nothing to add.
        return old_ch.id, new_id, None, "cancel_but_filled", {"already": already}
    if true_remaining != rq.quantity:
        rq = replace(rq, quantity=true_remaining)
    # Alpaca's cancel is ASYNC: the just-cancelled limit still holds the shares
    # (held_for_orders) for a beat, so an immediate re-place is rejected
    # "insufficient qty available" (prod RDGT 2026-08-10 left a subscriber long —
    # the forced close failed once and, with NO retry here, was never re-placed).
    # The reservation frees within ~1s — retry the place briefly, mirroring
    # _modify_place_one's cancel+place path.
    last_exc: BaseException | None = None
    for attempt in range(_MODIFY_PLACE_ATTEMPTS):
        try:
            return old_ch.id, new_id, ad.place_order(rq), None, {"already": already, "rq": rq}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if is_order_conflict_error(exc) and attempt < _MODIFY_PLACE_ATTEMPTS - 1:
                time.sleep(_MODIFY_PLACE_BACKOFF_S)
                continue
            break
    return old_ch.id, new_id, None, f"place_failed: {last_exc}"[:300], {"already": already, "rq": rq}


def force_fill_mirrors_to_market(trader_order_id: uuid.UUID) -> None:
    """A trader's order just FILLED — sweep any subscriber mirror of it that is
    STILL a resting limit to a market / marketable-limit fill, so the subscriber
    ends up in the SAME state as the trader. Applies to BOTH sides:
      * a filled SELL/close → force the subscriber's SELL so they EXIT too;
      * a filled BUY/entry  → force the subscriber's BUY so they get INTO the
        trade (their copied limit may not reach on the subscriber's venue).

    Background: while the trader's order is WORKING we mirror it as a cancellable
    limit (the ``trader_filled`` gate in _place_mirror_with_conflict_resolve), so
    a trader CANCEL just cancels the mirror instead of stranding a phantom fill.
    The flip side is this: when the trader's working order instead FILLS, we force
    any mirror whose limit hasn't filled to market — cancel the resting order,
    then place an immediate fill for the UNFILLED remainder (cancel-old +
    place-new, the same shape as a modify).

    NO-OP in the common cases — an order detected AT fill was already forced to
    market in fanout, and a mirror whose limit already filled is left alone. Only
    ever touches WORKING mirrors of THIS trader order, so it can never disturb
    unrelated trades. Opens its OWN session; safe to call from a listener fill
    hook after the trader row is committed."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    with SessionLocal() as db:
        # The trader's own fill price — anchors an extended-hours sweep limit to
        # where the trader actually traded (see _ext_hours_limit_price), instead of
        # our own possibly-divergent pre-market quote.
        _trader = db.get(Order, trader_order_id)
        trader_ref_price = _trader.filled_avg_price if _trader is not None else None
        children = list(db.execute(
            select(Order).where(
                Order.parent_order_id == trader_order_id,
                Order.status.in_(_WORKING_ORDER_STATUSES),
                Order.broker_order_id.isnot(None),
            )
        ).scalars())
        if not children:
            return

        # Build a cancel + immediate-fill plan for each still-resting mirror.
        plan: list[tuple[Order, Any, BrokerOrderRequest, uuid.UUID]] = []
        synced_any = False
        for ch in children:
            remaining = ch.quantity - (ch.filled_quantity or Decimal(0))
            if remaining <= 0:
                continue
            acct = db.get(BrokerAccount, ch.broker_account_id)
            if acct is None:
                continue
            try:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=ch.user_id,
                    action="order.mirror_force_fill_creds_error",
                    entity_type="order", entity_id=ch.id,
                    metadata={"parent_order_id": str(trader_order_id), "error": str(exc)[:300]},
                )
                continue
            # CRITICAL: ask the broker for THIS order's TRUE status before doing
            # anything. SnapTrade's cancel_order is UNRELIABLE — it sometimes
            # silently "succeeds" on an order that has already FILLED, which made
            # us mark a filled close as CANCELED and then reject a duplicate
            # replacement ("no position"). So if the broker says it already
            # FILLED, sync our row to FILLED and SKIP — never cancel/replace it.
            try:
                live = adapter.get_order(ch.broker_order_id)
            except Exception:  # noqa: BLE001
                live = None
            if live is not None and live.status == OrderStatus.FILLED:
                ch.status = OrderStatus.FILLED
                if live.filled_quantity is not None:
                    ch.filled_quantity = live.filled_quantity
                if live.filled_avg_price is not None:
                    ch.filled_avg_price = live.filled_avg_price
                if ch.closed_at is None:
                    ch.closed_at = datetime.now(timezone.utc)
                synced_any = True
                audit.record(
                    db, actor_user_id=ch.user_id,
                    action="order.mirror_force_fill_already_filled",
                    entity_type="order", entity_id=ch.id,
                    metadata={"parent_order_id": str(trader_order_id), "broker_order_id": ch.broker_order_id},
                )
                events.publish(ch.user_id, _order_event("order.placed", ch))
                continue
            # Honor any partial fill the broker reports — only sweep the rest.
            if live is not None and live.filled_quantity is not None and live.filled_quantity > (ch.filled_quantity or Decimal(0)):
                ch.filled_quantity = live.filled_quantity
                remaining = ch.quantity - live.filled_quantity
                if remaining <= 0:
                    synced_any = True
                    continue
            new_id = uuid.uuid4()
            # Immediate fill for the unfilled remainder — MARKET for a stock,
            # marketable-LIMIT for an option (via _to_immediate_close, which
            # prices through the market in the order's own direction: BUY → ask,
            # SELL → bid — so it works for entries and closes alike). is_closing
            # is preserved from the mirror so a SnapTrade close stays SELL_TO_CLOSE.
            req = BrokerOrderRequest(
                instrument_type=ch.instrument_type,
                symbol=ch.symbol,
                side=ch.side,
                order_type=ch.order_type,
                quantity=remaining,
                limit_price=ch.limit_price,
                stop_price=ch.stop_price,
                option_expiry=ch.option_expiry,
                option_strike=ch.option_strike,
                option_right=ch.option_right,
                is_closing=ch.is_closing,
                client_order_id=str(new_id),
            )
            req = _to_immediate_close(adapter, req, trader_ref_price)
            # Preserve the mirror's own is_closing (set above from ch) — do NOT
            # force it True, or an ENTRY would be sent as a *_TO_CLOSE action.
            if req.client_order_id != str(new_id):
                req = replace(req, client_order_id=str(new_id))
            plan.append((ch, adapter, req, new_id))

        if not plan:
            if synced_any:
                db.commit()  # persist the "already filled" status syncs above
            return

        # Phase 2 (thread pool): cancel the resting mirror, then place the close.
        with ThreadPoolExecutor(max_workers=min(32, len(plan))) as pool:
            results = list(pool.map(lambda it: _throttled_item(_force_fill_cancel_then_place, it), plan))

        # Phase 3 (session thread): apply.
        req_by_new_id = {new_id: rq for _c, _a, rq, new_id in plan}
        for old_id, new_id, resp, err, meta in results:
            old_ch = db.get(Order, old_id)
            if old_ch is None:
                continue
            # The request the worker ACTUALLY placed (re-sized to the true
            # remainder after re-reading the cancelled order's fill), else the
            # planned one for the no-op branches.
            rq = (meta or {}).get("rq") or req_by_new_id[new_id]
            if err == "cancel_but_filled":
                # The "cancelled" order actually FILLED the whole mirror during
                # Alpaca's async cancel window — placing anything would DOUBLE it
                # (the MSFT over-buy). Record the fill and place nothing.
                already = (meta or {}).get("already")
                if already is not None:
                    old_ch.filled_quantity = already
                old_ch.status = OrderStatus.FILLED
                if old_ch.closed_at is None:
                    old_ch.closed_at = datetime.now(timezone.utc)
                audit.record(
                    db, actor_user_id=old_ch.user_id,
                    action="order.mirror_force_fill_raced_fill",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(trader_order_id),
                              "broker_order_id": old_ch.broker_order_id,
                              "filled": str(already) if already is not None else None},
                )
                events.publish(old_ch.user_id, _order_event("order.placed", old_ch))
                continue
            if err == "cancel_noop_already_terminal":
                # The broker had nothing to cancel: the mirror already reached a
                # terminal state, i.e. it FILLED while we still believed it was
                # resting. Place nothing — this is the exact path that
                # double-bought a subscriber's META entry in prod — and do NOT
                # mark it CANCELED, because it isn't. fills_sync settles the real
                # status on its next tick.
                audit.record(
                    db, actor_user_id=old_ch.user_id,
                    action="order.mirror_force_fill_skipped_already_terminal",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(trader_order_id), "broker_order_id": old_ch.broker_order_id},
                )
                continue
            if err is not None and err.startswith("cancel_failed"):
                # Couldn't cancel — most likely the resting limit JUST filled on
                # its own (the subscriber exited at the better price). Leave it.
                audit.record(
                    db, actor_user_id=old_ch.user_id,
                    action="order.mirror_force_fill_cancel_failed",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(trader_order_id), "broker_order_id": old_ch.broker_order_id, "error": err},
                )
                continue
            if resp is None:
                # cancel_order returned OK but the REPLACEMENT failed. Do NOT
                # mark the old mirror CANCELED — SnapTrade's cancel can silently
                # "succeed" on an order that actually FILLED, and the place then
                # fails precisely because the position is already closed. Marking
                # it CANCELED (terminal) would mislabel a real fill and block
                # fill-sync from correcting it. Instead LEAVE the mirror as-is and
                # let fill-sync record its TRUE final status (FILLED or CANCELED)
                # from the broker. Also avoids leaving a phantom duplicate.
                audit.record(
                    db, actor_user_id=old_ch.user_id,
                    action="order.mirror_force_fill_place_failed",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(trader_order_id), "error": err, "left_for_fill_sync": True},
                )
                continue
            # BOTH cancel AND place succeeded — the old order was genuinely open,
            # is now cancelled at the broker, and the replacement is live. Only
            # NOW is it safe to mark the old mirror CANCELED.
            # Record any PARTIAL fill the cancelled order got before the cancel
            # landed, so the sub's net = that partial + the (remainder) child =
            # the full mirror — never double-counted, never lost.
            _already = (meta or {}).get("already")
            if _already is not None and _already > (old_ch.filled_quantity or Decimal(0)):
                old_ch.filled_quantity = _already
            old_ch.status = OrderStatus.CANCELED
            old_ch.closed_at = datetime.now(timezone.utc)
            events.publish(old_ch.user_id, _order_event("order.cancelled", old_ch))
            # Insert the NEW market/marketable fill row (same trader parent).
            new_child = Order(
                id=new_id,
                user_id=old_ch.user_id,
                broker_account_id=old_ch.broker_account_id,
                parent_order_id=trader_order_id,
                instrument_type=old_ch.instrument_type,
                symbol=old_ch.symbol,
                option_expiry=old_ch.option_expiry,
                option_strike=old_ch.option_strike,
                option_right=old_ch.option_right,
                is_closing=old_ch.is_closing,
                side=old_ch.side,
                order_type=rq.order_type,
                quantity=rq.quantity,
                limit_price=rq.limit_price,
                stop_price=rq.stop_price,
                take_profit_price=None,
                stop_loss_price=None,
                status=resp.status,
                broker_order_id=resp.broker_order_id,
                filled_quantity=resp.filled_quantity,
                filled_avg_price=resp.filled_avg_price,
                submitted_at=resp.submitted_at,
                broker_accepted_at=resp.submitted_at or datetime.now(timezone.utc),
                redis_published_at=datetime.now(timezone.utc),
            )
            db.add(new_child)
            audit.record(
                db, actor_user_id=new_child.user_id,
                action="order.mirror_force_filled_on_trader_fill",
                entity_type="order", entity_id=new_child.id,
                metadata={
                    "old_mirror_id": str(old_ch.id),
                    "parent_order_id": str(trader_order_id),
                    "broker_order_id": resp.broker_order_id,
                    "quantity": str(rq.quantity),
                },
            )
            db.flush()
            events.publish(new_child.user_id, _order_event("order.copy_submitted", new_child))
        db.commit()


def _deferred_close_query(sub_id, acct_id, it_type, sym, exp, strike, right):
    """Rows for a DEFERRED close on this exact contract (parked RETRY_PENDING with
    no broker order — see _DeferUntilEntryFills)."""
    return select(Order).where(
        Order.user_id == sub_id,
        Order.broker_account_id == acct_id,
        Order.instrument_type == it_type,
        Order.symbol == sym,
        Order.option_expiry.is_not_distinct_from(exp),
        Order.option_strike.is_not_distinct_from(strike),
        Order.option_right.is_not_distinct_from(right),
        Order.is_closing.is_(True),
        Order.status == OrderStatus.RETRY_PENDING,
        Order.broker_order_id.is_(None),
    )


def fire_deferred_closes_for_entry(entry: Order) -> None:
    """A subscriber ENTRY just FILLED — place any close we DEFERRED for this exact
    contract (a close that arrived before its entry filled, e.g. a pre-market SELL
    queued behind a BUY, or a fast scalp). Now that the position exists, the close
    can go through. Called from the fill-detection paths (Alpaca fills_sync +
    SnapTrade subscriber reconciler) on a mirror BUY→FILLED transition. Opens its
    OWN session and reads the LIVE broker position, so it doesn't depend on the
    caller's transaction. NO-OP when there's nothing deferred."""
    if entry.is_closing or entry.parent_order_id is None:
        return
    sub_id, acct_id = entry.user_id, entry.broker_account_id
    it_type, sym = entry.instrument_type, entry.symbol
    exp, strike, right = entry.option_expiry, entry.option_strike, entry.option_right
    with SessionLocal() as db:
        deferred = list(db.execute(
            _deferred_close_query(sub_id, acct_id, it_type, sym, exp, strike, right)
        ).scalars().all())
        if not deferred:
            return
        acct = db.get(BrokerAccount, acct_id)
        if acct is None:
            return
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
        except Exception as exc:  # noqa: BLE001
            log.exception("fire_deferred_closes: adapter build failed for %s", acct_id)
            return
        for ch in deferred:
            req = BrokerOrderRequest(
                instrument_type=ch.instrument_type, symbol=ch.symbol, side=ch.side,
                order_type=ch.order_type, quantity=ch.quantity, limit_price=ch.limit_price,
                stop_price=ch.stop_price, option_expiry=ch.option_expiry,
                option_strike=ch.option_strike, option_right=ch.option_right,
                is_closing=True, client_order_id=str(ch.id),
            )
            # Clamp to what's actually held now; the entry may have partially filled.
            held = live_closeable_quantity(adapter, req)
            if held is not None and held <= 0:
                continue  # position still not there — leave for the retry-scheduler TTL
            if held is not None and held < req.quantity:
                req = replace(req, quantity=held)
            req = _to_immediate_close(adapter, req)
            if not req.is_closing:
                req = replace(req, is_closing=True)
            try:
                resp = adapter.place_order(req)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=ch.user_id, action="copy.deferred_close_place_failed",
                    entity_type="order", entity_id=ch.id,
                    metadata={"symbol": ch.symbol, "error": str(exc)[:300]},
                )
                continue
            ch.status = resp.status
            ch.broker_order_id = resp.broker_order_id
            ch.order_type = req.order_type
            ch.quantity = req.quantity
            ch.limit_price = req.limit_price
            ch.filled_quantity = resp.filled_quantity
            ch.filled_avg_price = resp.filled_avg_price
            ch.submitted_at = resp.submitted_at
            ch.retry_at = None
            ch.reject_reason = None
            audit.record(
                db, actor_user_id=ch.user_id, action="copy.deferred_close_placed_on_entry_fill",
                entity_type="order", entity_id=ch.id,
                metadata={"symbol": ch.symbol, "broker_order_id": resp.broker_order_id, "quantity": str(req.quantity)},
            )
            events.publish(ch.user_id, _order_event("order.placed", ch))
        db.commit()


def cancel_deferred_closes_for_entry(entry: Order) -> None:
    """A subscriber ENTRY died (CANCELED / REJECTED / EXPIRED) without filling — so
    any close we DEFERRED behind it has nothing to sell. Mark those deferred
    closes CANCELED. Own session; NO-OP when nothing is deferred."""
    if entry.is_closing or entry.parent_order_id is None:
        return
    sub_id, acct_id = entry.user_id, entry.broker_account_id
    it_type, sym = entry.instrument_type, entry.symbol
    exp, strike, right = entry.option_expiry, entry.option_strike, entry.option_right
    with SessionLocal() as db:
        deferred = list(db.execute(
            _deferred_close_query(sub_id, acct_id, it_type, sym, exp, strike, right)
        ).scalars().all())
        if not deferred:
            return
        now = datetime.now(timezone.utc)
        for ch in deferred:
            ch.status = OrderStatus.CANCELED
            ch.closed_at = now
            ch.retry_at = None
            ch.reject_reason = "Your entry never filled, so there was nothing to close."[:480]
            audit.record(
                db, actor_user_id=ch.user_id, action="copy.deferred_close_cancelled_entry_died",
                entity_type="order", entity_id=ch.id, metadata={"symbol": ch.symbol},
            )
            events.publish(ch.user_id, _order_event("order.cancelled", ch))
        db.commit()


def _leg_direction(side: OrderSide, leg: str) -> Decimal:
    """+1 when a correctly-placed leg sits ABOVE entry, -1 when BELOW.
    Mirrors the frontend InlineBracketCell convention:
      buy+tp / sell+sl → +1 ; buy+sl / sell+tp → -1."""
    buy = side == OrderSide.BUY
    positive = (buy and leg == "tp") or (not buy and leg == "sl")
    return Decimal("1") if positive else Decimal("-1")


def _trader_bracket_for_copy(trader_order: Order) -> tuple[bool, Decimal | None, Decimal | None]:
    """Describe how to stamp the trader's bracket onto a copied subscriber
    entry. Returns ``(use_pct, tp_val, sl_val)``:

      * ``use_pct=True``  → tp_val / sl_val are POSITIVE percent distances
        from entry; the bracket emulator re-anchors them on the
        subscriber's own fill so each subscriber gets the same risk/reward
        % regardless of their fill price or multiplier.
      * ``use_pct=False`` → tp_val / sl_val are ABSOLUTE prices, a fallback
        used only when the trader order has no usable entry reference yet
        (e.g. an unfilled market order with no limit price). The exits then
        match the trader's price levels verbatim.

    Either leg is None when the trader didn't set it (or it computed to a
    non-positive / inverted percent, which we drop rather than place an
    exit on the wrong side)."""
    tp_price = trader_order.take_profit_price
    sl_price = trader_order.stop_loss_price
    if tp_price is None and sl_price is None:
        return (False, None, None)

    entry_ref = trader_order.limit_price or trader_order.filled_avg_price
    if not entry_ref or entry_ref <= 0:
        # No anchor to derive a percent from → copy absolute prices.
        return (False, tp_price, sl_price)

    q = Decimal("0.0001")
    tp_pct: Decimal | None = None
    sl_pct: Decimal | None = None
    if tp_price is not None:
        pct = _leg_direction(trader_order.side, "tp") * (tp_price / entry_ref - 1) * 100
        tp_pct = pct.quantize(q) if pct > 0 else None
    if sl_price is not None:
        pct = _leg_direction(trader_order.side, "sl") * (sl_price / entry_ref - 1) * 100
        sl_pct = pct.quantize(q) if pct > 0 else None
    return (True, tp_pct, sl_pct)


def trader_can_trade(db: Session, trader: User) -> bool:
    if trader.role != UserRole.TRADER:
        return False
    settings = db.get(TraderSettings, trader.id)
    return bool(settings and settings.trading_enabled)


# ── Async fanout (the live path used by BackgroundTasks) ──────────────────


async def fanout_async(db: Session, trader_order: Order, trader: User) -> list[FanoutResult]:
    """Mirror `trader_order` to all subscribers, broker calls run concurrently.

    Phase 1 + 3 are DB-bound and run on the calling coroutine (no DB sharing
    across threads). Phase 2 awaits asyncio.gather over per-mirror place_order
    coroutines; each wraps the sync SDK in asyncio.to_thread under a per-broker
    semaphore.

    Caller commits the session.
    """
    results: list[FanoutResult] = []
    pending: list[_PendingMirror] = []

    # Bracket-leg guard. Emulator-spawned TP/SL exits (bracket_parent_id
    # set) are trader-only by design — each subscriber's own listener
    # runs the bracket emulator on their own mirrored entry and
    # generates their own exits at the right size. Broadcasting the
    # trader's exits would double-close and use the trader's quantity
    # instead of each subscriber's scaled fill. The emulator already
    # marks these fanned_out=True at creation; this is defence-in-depth
    # in case anything else (a backfill, a manual replay) hands us one.
    if trader_order.bracket_parent_id is not None:
        return results

    # Discord broadcast — a trader order that is ALREADY filled at detection
    # (a market fill, or first-seen-filled by a listener/poll). This is the
    # single point EVERY trader order flows through (Trade Panel + all
    # listeners), so it catches the case the per-listener fill-transition hooks
    # miss: an order that was already FILLED before we ever tracked it (e.g. a
    # Trade-Panel market order that filled at placement — no working→filled
    # transition ever fires). Deduped + gated inside — including a copy_paused
    # gate, so no card is broadcast while the trader's copy trading is OFF (the
    # trades aren't being mirrored). Delayed limit fills are covered by the
    # listeners' transition hooks.
    if trader_order.status == OrderStatus.FILLED:
        try:
            from app.services import discord_alerts  # noqa: PLC0415
            discord_alerts.emit_trader_fill_alert(trader_order.id)
        except Exception:  # noqa: BLE001
            log.exception("discord alert (fanout) failed for %s", trader_order.id)

    # Trader master pause. We DON'T skip the whole fanout here anymore: while
    # paused, the trader's OPENS are dropped (no new entries for anyone), but
    # their CLOSES must still flow to subscribers — otherwise a sub is left
    # holding a position the trader has already exited (prod: trader paused
    # copy, then closed his positions, subs stayed long). The actual gate is
    # applied once we know whether this order is a close (`trader_closing`) —
    # see the `trader_paused and not trader_closing` return below; and
    # `is_close_only` is OR'd with `trader_paused` so every holder's exit
    # mirrors close-only.
    ts = db.get(TraderSettings, trader.id)
    trader_paused = ts is not None and ts.copy_paused

    # Reliable "the trader is CLOSING" signal — from the TRADER's OWN held
    # position (their fills sync promptly via the trader listener, unlike a
    # subscriber's which can lag on SnapTrade). Computed here (early) so a paused
    # OPEN can short-circuit BEFORE any Phase-1 side effects (the daily
    # auto-resume sweep, subs fetch), keeping the paused-open path byte-for-byte
    # as it was before this change. Reused throughout the fanout below.
    trader_closing = bool(trader_order.is_closing) or _closeable_quantity(
        db, trader_order.user_id, trader_order, subtract_reserved=False,
        # Exclude this order from the net: a FULL close (position → flat) counts
        # its OWN fill and reads 0 ("not closing"), which — while paused — dropped
        # the whole fanout and left subscribers holding a position the trader had
        # exited. Excluding it yields the position that existed BEFORE this order.
        exclude_order_id=trader_order.id,
    ) > 0
    # Did this order take the TRADER fully FLAT for the contract? Net position
    # INCLUDING this order == 0 (and it was a close, not a naked short open). Used
    # below so a subscriber whose fractional-multiplier close rounded DOWN to 0
    # (e.g. mult 0.25, trader closing 1-2 contracts) still fully exits when the
    # trader does — otherwise the sub is stranded holding a position the trader has
    # left (prod 2026-08: skipped_zero_qty on SELLs). Computed once per fanout.
    trader_fully_flat = trader_closing and _closeable_quantity(
        db, trader_order.user_id, trader_order, subtract_reserved=False,
    ) == 0
    # Trader master pause: while paused we drop OPENS (no new entries for anyone)
    # but STILL mirror CLOSES so a subscriber isn't left holding a position the
    # trader has exited. Paused + OPENING → skip the whole fanout here (exactly
    # as before). Paused + CLOSING → fall through and run close-only for everyone
    # (is_close_only is OR'd with trader_paused below; the _closeable_quantity
    # clamp zeroes out anyone who doesn't hold it, so it can never open a naked
    # position).
    if trader_paused and not trader_closing:
        return results

    # ── Phase 1: build child orders + skip records ─────────────────────────
    subs = await cache.get_subscribers_for_trader(db, trader.id)

    # ── Daily auto-resume sweep ────────────────────────────────────────────
    # For every subscriber whose copy was paused by a DAILY limit
    # (daily_loss_limit / daily_profit_limit / max_account_pct_per_day,
    # plus their _pct variants — all stamp `pnl_auto_paused_at`), check
    # whether the pause was set on a PRIOR UTC day. If so, clear the
    # pause + re-enable copy_enabled so today's trades flow. Keying off
    # `pnl_auto_paused_at` (not just `copy_enabled=False`) means a
    # subscriber who manually paused their own copy won't be re-enabled
    # — only auto-pauses come back.
    #
    # Auto-liquidation (`auto_liquidation_limit`) uses a DIFFERENT column
    # (`auto_liquidated_at`) and is not affected by this sweep —
    # liquidation stays sticky until the subscriber manually re-enables
    # copy. That's the intentional split: daily limits forgive on the
    # next day, hard-equity liquidation does not.
    #
    # We also run the matching sweep in pnl_poller so an idle subscriber
    # (one whose trader hasn't placed any orders today) still auto-resumes
    # on schedule. Both sweeps clear `pnl_auto_paused_at` on success so
    # they're idempotent against each other.
    today_utc = datetime.now(timezone.utc).date()
    resumed_user_ids: list[uuid.UUID] = []
    for sub in subs:
        paused_iso = getattr(sub, "pnl_auto_paused_at", None)
        if not paused_iso:
            continue
        try:
            paused_at = datetime.fromisoformat(paused_iso) if isinstance(paused_iso, str) else paused_iso
        except ValueError:
            continue
        if paused_at.astimezone(timezone.utc).date() < today_utc:
            db_settings = db.get(SubscriberSettings, sub.user_id)
            if db_settings is not None:
                db_settings.copy_enabled = True
                db_settings.pnl_auto_paused_at = None
                resumed_user_ids.append(sub.user_id)
                audit.record(
                    db,
                    actor_user_id=sub.user_id,
                    action="copy.auto_resumed_next_day",
                    entity_type="subscriber_settings",
                    entity_id=sub.user_id,
                    metadata={"paused_at": str(paused_iso), "resumed_at": today_utc.isoformat()},
                )
                events.publish(sub.user_id, {
                    "type": "copy.auto_resumed",
                    "reason": "new_day",
                })
    if resumed_user_ids:
        # Re-fetch the active subscriber list AFTER flipping copy_enabled
        # so the per-sub loop below sees the freshly-resumed users this
        # very fanout (otherwise they'd need a second trade to fire).
        cache.invalidate_subscribers_for_trader(trader.id)
        subs = await cache.get_subscribers_for_trader(db, trader.id)

    # Decide hybrid path first — we need it to know whether to do the
    # batched broker_accounts SELECT (we skip it for small-N to keep the
    # per-iter path's low floor intact).
    threshold = await get_fanout_batch_threshold_async()
    use_batch = len(subs) >= threshold

    # PRE-PHASE-1 PARALLEL BATCHES — these two prep steps are independent
    # and previously ran serially:
    #   (1) today_realized_pnl_bulk — FIFO lot-walk for every subscriber
    #       with a P&L limit set. The single most expensive piece of prep
    #       (often 150-250 ms at scale).
    #   (2) batched broker_accounts SELECT — only in the batched path.
    # Wrapping both in asyncio.gather lets them overlap, so the slower of
    # the two sets the floor instead of (1) + (2) added together.
    #
    # NOTE: previous revisions also fetched a `users_by_id` dict just to
    # do `if not sub_user: continue`. That guard never fires in practice —
    # get_subscribers_for_trader() returns only subscribers whose
    # SubscriberSettings row exists, which CASCADEs from users, so a
    # returned sub.user_id is guaranteed to correspond to a live User.
    # Dropping that SELECT saves another ~30-50 ms.
    sub_ids_with_limit = [
        s.user_id for s in subs
        if s.daily_loss_limit is not None or s.daily_profit_limit is not None
    ]
    sub_user_ids = [s.user_id for s in subs] if use_batch else []

    # Each parallel branch opens its OWN SessionLocal — SQLAlchemy
    # sessions aren't safe to share across threads, and to_thread can run
    # both branches concurrently. The caller's `db` keeps the
    # transactional context for everything after this gather (Phase 1
    # inserts, Phase 3 commit).
    def _pnl_sync() -> dict[uuid.UUID, Decimal]:
        if not sub_ids_with_limit:
            return {}
        with SessionLocal() as session:
            return today_realized_pnl_bulk(session, sub_ids_with_limit)

    def _accts_sync() -> dict[uuid.UUID, list[BrokerAccount]]:
        d: dict[uuid.UUID, list[BrokerAccount]] = defaultdict(list)
        if not sub_user_ids:
            return d
        with SessionLocal() as session:
            for acct in session.execute(
                select(BrokerAccount).where(BrokerAccount.user_id.in_(sub_user_ids))
            ).scalars():
                # Detach so the BrokerAccount survives past the session
                # close — we read attributes (encrypted_credentials,
                # supports_fractional, broker, id) inside the loop on
                # the caller's coroutine, after this session exits.
                session.expunge(acct)
                d[acct.user_id].append(acct)
            return d

    pnl_by_user, accts_by_user = await asyncio.gather(
        asyncio.to_thread(_pnl_sync),
        asyncio.to_thread(_accts_sync),
    )

    # The trader's bracket is identical for every subscriber, so resolve it
    # ONCE here. Only subscribers with copy_trader_bracket=True consume it
    # (see the child construction below). use_pct chooses re-anchored-percent
    # vs absolute-price copy; see _trader_bracket_for_copy.
    copy_use_pct, copy_tp_val, copy_sl_val = _trader_bracket_for_copy(trader_order)

    # End-of-day lockout — PER-SUBSCRIBER (each has their own opt-in toggle and
    # 1–30 min window). Compute the order-level part once; the per-subscriber
    # enabled/window check happens in the loop below. We refuse a subscriber's
    # NEW same-day-expiry option mirror inside THEIR window because eod_autoclose
    # is flattening those very contracts then — letting a fresh 0DTE mirror
    # through would re-strand them (or no-op a close against an already-flattened
    # position). Later-expiry options and all stocks pass through untouched.
    eod_candidate = (
        get_settings().eod_autoclose_enabled
        and trader_order.instrument_type == InstrumentType.OPTION
        and market_hours.is_same_day_expiry(trader_order.option_expiry)
    )
    eod_now = market_hours.now_et() if eod_candidate else None

    # `trader_closing` and the trader-master-pause short-circuit were computed
    # ABOVE (right after the ts fetch) so a paused OPEN skips the fanout before
    # any Phase-1 side effects; trader_closing is reused here.

    # ── Close-through-pause: mirror the trader's EXITS to PAUSED subscribers ──
    # A subscriber with copy_enabled=False (manual off OR daily-limit
    # auto-pause) is normally absent from `subs` entirely — the cache query
    # only returns active subscribers. That strands them holding a position
    # after the trader has already exited. So when the trader is CLOSING,
    # pull their still-following-but-paused subscribers back in, flagged
    # close-only: the per-sub loop below skips every entry path for them and
    # lets ONLY closes of positions they actually hold through (the existing
    # _closeable_quantity clamp zeroes out anything they don't hold, so this
    # can never place a naked sell). New entries stay blocked while paused.
    # Gated on `trader_closing` so the common entry path pays nothing.
    paused_close_subs: list = []
    if trader_closing:
        paused_rows = db.execute(
            select(SubscriberSettings).where(
                SubscriberSettings.following_trader_id == trader.id,
                SubscriberSettings.copy_enabled.is_(False),
            )
        ).scalars().all()
        for row in paused_rows:
            # Non-mapped marker read via getattr(..., False) in the loop.
            row._close_only = True
        paused_close_subs = list(paused_rows)

    # Fresh per-contract caps, read straight from the DB — NOT the cached
    # subscriber list. A just-set max_per_contract can lag in the fanout cache
    # (invalidation race / a concurrent read repopulating the key with the
    # pre-cap value) by up to the cache TTL, which let over-cap OPTION opens slip
    # through in QA (2026-08-24: cap set, 28s later an over-cap open still filled).
    # A risk cap must apply on the very NEXT trade, so for option orders we
    # authoritatively fetch every relevant cap once here and the gate reads this
    # map instead of the cached subscriber. One cheap indexed local query, only
    # for options.
    _fresh_caps: dict[uuid.UUID, Decimal] = {}
    if trader_order.instrument_type == InstrumentType.OPTION:
        _cap_ids = [s.user_id for s in (*subs, *paused_close_subs)]
        if _cap_ids:
            for _uid, _cap in db.execute(
                select(
                    SubscriberSettings.user_id, SubscriberSettings.max_per_contract,
                ).where(
                    SubscriberSettings.user_id.in_(_cap_ids),
                    SubscriberSettings.max_per_contract.isnot(None),
                )
            ).all():
                _fresh_caps[_uid] = _cap

    # Price the max-per-contract gate evaluates against. Prefer the trader's own
    # premium; for a MARKET option fanned out BEFORE its fill commits that isn't
    # recorded yet (filled_avg_price None, no limit_price) — which used to fail
    # the cap OPEN and let over-cap options through (QA 2026-08) — fall back to a
    # live option quote. Computed ONCE here (the contract's premium is the same
    # for every subscriber), and only when some subscriber actually has a cap.
    _gate_px: "Decimal | None" = trader_order.filled_avg_price or trader_order.limit_price
    if _gate_px is None and _fresh_caps:
        _gate_px = _live_option_premium(db, trader, trader_order)

    for sub in [*subs, *paused_close_subs]:
        # Paused subscribers are admitted CLOSE-ONLY: skip every entry-side
        # gate (EOD lockout, daily kill switch, symbol filters) and, in the
        # per-account block, refuse anything that isn't a real close.
        is_close_only = getattr(sub, "_close_only", False) or trader_paused
        # Lifecycle: the moment the engine picks this subscriber up for
        # processing. Applied to every child Order created in this iteration
        # below. Captured here (not inside the inner per-account loop) so it
        # reflects the per-subscriber pick, not per-account. After batching,
        # all picked_at values are within microseconds — pick_lag is now a
        # platform-overhead floor, not a queue-position artifact.
        subscriber_picked_at = datetime.now(timezone.utc)

        # EOD lockout: refuse this subscriber's new same-day-expiry option mirror
        # only if THEY opted in and we're inside THEIR window (see eod_candidate).
        # Skipped for close-only (paused) subs — a close must never be blocked.
        if (
            not is_close_only
            and eod_candidate
            and getattr(sub, "eod_autoclose_enabled", False)
            and market_hours.in_eod_close_window(
                eod_now, minutes=getattr(sub, "eod_autoclose_minutes", 15)
            )
        ):
            audit.record(
                db,
                actor_user_id=sub.user_id,
                action="copy.skipped_eod_same_day_expiry",
                entity_type="order",
                entity_id=trader_order.id,
                metadata={
                    "symbol": trader_order.symbol,
                    "option_expiry": str(trader_order.option_expiry),
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=sub.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_eod_same_day_expiry",
            ))
            continue

        # Daily P&L kill switches (check BEFORE placing). Loss + profit
        # share the same auto-pause path — both stamp pnl_auto_paused_at
        # as an audit marker. Re-enable is MANUAL ONLY (Settings UI).
        # Skipped for close-only subs — they're ALREADY paused; re-running
        # the kill switch would just re-stamp, and we're only letting their
        # exits through anyway.
        if not is_close_only and (sub.daily_loss_limit is not None or sub.daily_profit_limit is not None):
            todays_pnl = pnl_by_user.get(sub.user_id, Decimal(0))
            hit_loss = (
                sub.daily_loss_limit is not None
                and todays_pnl <= -sub.daily_loss_limit
            )
            hit_profit = (
                sub.daily_profit_limit is not None
                and todays_pnl >= sub.daily_profit_limit
            )
            if hit_loss or hit_profit:
                reason = "daily_loss_limit" if hit_loss else "daily_profit_limit"
                now_utc = datetime.now(timezone.utc)
                # Flip the DB row off + stamp pnl_auto_paused_at as the
                # audit marker for "auto-paused at this time". The
                # subscriber re-enables manually from the Settings UI.
                db_settings = db.get(SubscriberSettings, sub.user_id)
                if db_settings is not None:
                    db_settings.copy_enabled = False
                    db_settings.pnl_auto_paused_at = now_utc
                cache.invalidate_subscribers_for_trader(trader.id)
                audit.record(
                    db,
                    actor_user_id=sub.user_id,
                    action=f"copy.auto_paused_{reason}",
                    entity_type="subscriber_settings",
                    entity_id=sub.user_id,
                    metadata={
                        "daily_loss_limit": str(sub.daily_loss_limit) if sub.daily_loss_limit else None,
                        "daily_profit_limit": str(sub.daily_profit_limit) if sub.daily_profit_limit else None,
                        "todays_realized_pnl": str(todays_pnl),
                        "trigger_order_id": str(trader_order.id),
                    },
                )
                events.publish(sub.user_id, {
                    "type": "copy.auto_paused",
                    "reason": reason,
                    "daily_loss_limit": str(sub.daily_loss_limit) if sub.daily_loss_limit else None,
                    "daily_profit_limit": str(sub.daily_profit_limit) if sub.daily_profit_limit else None,
                    "todays_realized_pnl": str(todays_pnl),
                })
                results.append(FanoutResult(
                    subscriber_user_id=sub.user_id,
                    broker_account_id=uuid.UUID(int=0),
                    order_id=None,
                    status=f"skipped_{reason}",
                ))
                continue

        # Per-subscriber symbol filter (exclusion / inclusion lists).
        # Checked BEFORE broker-account lookup so a fully-filtered trade
        # short-circuits cheaply. Symbol comparison is uppercase on both
        # sides — _normalize_symbols enforces uppercase storage, but
        # trader_order.symbol can come from broker callbacks where casing
        # is unpredictable.
        # Skipped for close-only subs — an exit must fire even for a symbol
        # the subscriber has filtered out of NEW entries.
        trade_symbol = (trader_order.symbol or "").upper()
        excl = () if is_close_only else (sub.symbol_exclusion_list or ())
        incl = () if is_close_only else (sub.symbol_inclusion_list or ())
        if excl and trade_symbol in {s.upper() for s in excl}:
            audit.record(
                db,
                actor_user_id=sub.user_id,
                action="copy.skipped_excluded_symbol",
                entity_type="order",
                entity_id=trader_order.id,
                metadata={"symbol": trade_symbol, "rule": "exclusion_list"},
            )
            results.append(FanoutResult(
                subscriber_user_id=sub.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_excluded_symbol",
            ))
            continue
        if incl and trade_symbol not in {s.upper() for s in incl}:
            audit.record(
                db,
                actor_user_id=sub.user_id,
                action="copy.skipped_not_in_inclusion_list",
                entity_type="order",
                entity_id=trader_order.id,
                metadata={"symbol": trade_symbol, "rule": "inclusion_list"},
            )
            results.append(FanoutResult(
                subscriber_user_id=sub.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_not_in_inclusion_list",
            ))
            continue

        # Hybrid: dict lookup when pre-batched, per-iter cache call otherwise.
        # Close-only (paused) subs are NOT in the pre-batched dict (that was
        # built from the active `subs` list), so always resolve them via the
        # per-sub cache call.
        sub_accounts = (
            accts_by_user.get(sub.user_id, [])
            if (use_batch and not is_close_only)
            else await cache.get_broker_accounts(db, sub.user_id)
        )
        if not sub_accounts:
            results.append(FanoutResult(
                subscriber_user_id=sub.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_no_broker",
            ))
            continue

        for acct in sub_accounts:
            # ── Externally-placed bracket handling (SnapTrade/Webull) ──
            # A trader bracket arrives as 3 linked orders: a TRIGGER entry (which
            # our listener stamps with take_profit_price/stop_loss_price) and its
            # CONDITIONAL exit legs (bracket_parent_id set). For ALPACA stock
            # subscribers we reproduce it as a single NATIVE bracket on the entry
            # — so the exits go in with the entry and arm on fill (Alpaca rejects
            # 3 separate opposite-side orders as a wash trade). Scoped to Alpaca +
            # stocks for now; every other broker/instrument keeps prior behavior.
            is_alpaca = acct.broker == BrokerName.ALPACA
            is_stock = trader_order.instrument_type == InstrumentType.STOCK
            is_exit_leg = trader_order.bracket_parent_id is not None
            # Mirror the trader's exit(s) natively on the entry: BOTH legs → an
            # Alpaca bracket, ONE leg → an OTO (the adapter picks the class). So
            # we go native whenever AT LEAST ONE exit is attached.
            alpaca_native_bracket = (
                is_alpaca
                and is_stock
                and not is_exit_leg
                and (
                    trader_order.take_profit_price is not None
                    or trader_order.stop_loss_price is not None
                )
            )
            # Don't mirror a STOCK exit leg to Alpaca — the native bracket on the
            # entry already carries it. Non-Alpaca accounts, and option legs
            # (Alpaca has no native option bracket), keep the prior behavior.
            if is_exit_leg and is_alpaca and is_stock:
                continue

            scaled = _scale_quantity(
                trader_order.quantity, sub.multiplier, acct.supports_fractional
            )
            # ── Determine whether THIS is a close for the subscriber ──
            # The broker's is_closing flag is only reliable for OPTIONS (SnapTrade
            # sets SELL_TO_CLOSE); for STOCKS it's ALWAYS False (SnapTrade just
            # says "SELL"). So we also treat any order that REDUCES the
            # subscriber's own held position for this exact contract as a close.
            # This drives BOTH the close-clamp below AND the immediate-fill close
            # behavior in _place_mirror_with_conflict_resolve — without it, stock
            # closes would keep the trader's limit and get stuck unfilled.
            # We CANNOT trust the broker's is_closing flag. SnapTrade reports
            # Webull actions as plain BUY/SELL (no _TO_OPEN/_TO_CLOSE), so a real
            # close arrives with is_closing=False — for OPTIONS as well as stocks
            # (confirmed in prod: every Webull-Canada order had is_closing=f,
            # which made mirror SELLs get sent as opening sells and rejected as
            # "no position to close" / "insufficient buying power"). So detect a
            # close the reliable, broker-agnostic way for BOTH instruments — by
            # whether the subscriber actually holds a position this order reduces.
            # _closeable_quantity matches the exact option contract and nets by
            # direction, so an entry (nothing held to reduce) returns 0.
            closeable = _closeable_quantity(
                db, sub.user_id, trader_order, subtract_reserved=False,
            )
            is_closing_effective = bool(trader_order.is_closing) or closeable > 0

            # Close-only (paused) subscriber: admit ONLY genuine closes. An
            # entry (is_closing_effective False) is dropped here so a paused
            # subscriber never opens a new position; their exits still flow.
            if is_close_only and not is_closing_effective:
                results.append(FanoutResult(
                    subscriber_user_id=sub.user_id,
                    broker_account_id=acct.id,
                    order_id=None,
                    status="skipped_paused_entry",
                ))
                continue

            # ── Max per-contract value gate (OPTION opens only) ──────────────
            # Skip an OPENING option mirror when a single contract's value
            # (premium × 100) exceeds the subscriber's max_per_contract ceiling.
            # A CLOSE always passes (they must be able to exit); stocks have no
            # per-contract concept, so this is options-only. Priced off _gate_px
            # (trader's premium, or a live option quote when a market order is
            # fanned out before its fill records a price); only when NEITHER is
            # available does it fall back to allow. Read the cap from the FRESH
            # DB map (see _fresh_caps above), never the cached subscriber, so a
            # just-set cap can't be missed.
            _mpc = _fresh_caps.get(sub.user_id)
            if (
                not is_closing_effective
                and _mpc is not None
                and trader_order.instrument_type == InstrumentType.OPTION
            ):
                _px = _gate_px  # trader premium, or a live quote for a pre-fill market option
                if _px is not None and Decimal(_px) * Decimal(100) > Decimal(_mpc):
                    audit.record(
                        db, actor_user_id=sub.user_id,
                        action="copy.skipped_max_per_contract",
                        entity_type="order", entity_id=trader_order.id,
                        metadata={
                            "symbol": trade_symbol,
                            "per_contract": str(Decimal(_px) * Decimal(100)),
                            "max_per_contract": str(_mpc),
                        },
                    )
                    results.append(FanoutResult(
                        subscriber_user_id=sub.user_id,
                        broker_account_id=acct.id,
                        order_id=None,
                        status="skipped_max_per_contract",
                    ))
                    continue

            # Defense-in-depth: never mirror a CLOSE larger than the subscriber
            # actually holds. Clamp to the raw held position (not minus reserved):
            # if a working order reserves it, the conflict-resolve retry in
            # _place_mirror_with_conflict_resolve cancels that order and re-places,
            # rather than shrinking the close to zero here.
            if is_closing_effective and scaled > 0:
                if closeable < scaled:
                    # Clamping a close to what the subscriber holds is correct for
                    # a real partial. BUT shrinking it to ZERO is usually the
                    # FILL-SYNC RACE: the subscriber's entry filled at the broker
                    # (e.g. a fast buy→sell, or a pre-market fill) and just hasn't
                    # synced to our filled_quantity yet, so _closeable_quantity
                    # reads 0. Two shapes of this race, both of which must NOT drop
                    # the close (leave `scaled` as-is; placement re-checks the LIVE
                    # broker position and defers/closes accordingly):
                    #   1. a same-contract ENTRY is still WORKING — the fill is on
                    #      its way (pre-market BUY, or a fast scalp where SELL beat
                    #      BUY). Skipped a subscriber's NVDA close in prod.
                    #   2. the entry reached FILLED but its filled_quantity hasn't
                    #      synced (SnapTrade reports status ahead of quantity), AND
                    #      the TRADER is genuinely closing — so the broker DOES hold
                    #      it, our count just lags. Skipped a subscriber's NDXP close
                    #      on Webull while the trader had already exited.
                    # A rejected entry (e.g. Alpaca can't trade an index option) is
                    # neither working nor filled, so a genuine 'never acquired'
                    # close is still cleanly skipped below.
                    entry_landing = _has_working_entry_for_contract(
                        db, sub.user_id, trader_order
                    )
                    entry_filled_unsynced = trader_closing and _has_filled_entry_for_contract(
                        db, sub.user_id, trader_order
                    )
                    if closeable <= 0 and (entry_landing or entry_filled_unsynced):
                        # We KNOW this is a close (working/filled entry + trader
                        # exiting), so force close semantics — SnapTrade needs
                        # SELL_TO_CLOSE, and it makes the broker safely reject
                        # rather than open a short if we're somehow already flat.
                        is_closing_effective = True
                        audit.record(
                            db,
                            actor_user_id=sub.user_id,
                            action="copy.close_entry_pending_no_clamp",
                            entity_type="order",
                            entity_id=trader_order.id,
                            metadata={
                                "requested_qty": str(scaled),
                                "symbol": trader_order.symbol,
                                "broker_account_id": str(acct.id),
                                "reason": "entry_filled_unsynced" if entry_filled_unsynced and not entry_landing else "entry_working",
                            },
                        )
                    else:
                        audit.record(
                            db,
                            actor_user_id=sub.user_id,
                            action="copy.close_clamped",
                            entity_type="order",
                            entity_id=trader_order.id,
                            metadata={
                                "requested_qty": str(scaled),
                                "held_qty": str(closeable),
                                "symbol": trader_order.symbol,
                                "broker_account_id": str(acct.id),
                            },
                        )
                        scaled = closeable
            # Full-exit override: when the trader has FULLY closed this contract,
            # the subscriber closes their ENTIRE held position — regardless of the
            # multiplier-scaled qty. Without this, a fractional multiplier that
            # rounds a (small/incremental) close DOWN to 0 skips the sub's SELL and
            # strands them holding a position the trader has already exited (prod:
            # mult 0.25/0.5, closing 1-2 contracts → skipped_zero_qty). Only when
            # they actually hold something (closeable > 0); partial closes keep
            # their proportional scaling and self-correct on the trader's full exit.
            if is_closing_effective and trader_fully_flat and closeable > 0:
                scaled = closeable
            if scaled <= 0:
                audit.record(
                    db,
                    actor_user_id=sub.user_id,
                    action="copy.skipped_zero_qty",
                    entity_type="order",
                    entity_id=trader_order.id,
                    metadata={
                        "trader_qty": str(trader_order.quantity),
                        "multiplier": str(sub.multiplier),
                        "broker_account_id": str(acct.id),
                    },
                )
                results.append(FanoutResult(
                    subscriber_user_id=sub.user_id,
                    broker_account_id=acct.id,
                    order_id=None,
                    status="skipped_zero_qty",
                ))
                continue

            # Lifecycle: passed all eligibility checks (no daily-loss kill,
            # has broker accounts, scaled qty > 0). About to insert the child
            # row and call the broker.
            subscriber_accepted_at = datetime.now(timezone.utc)

            child = Order(
                id=uuid.uuid4(),
                user_id=sub.user_id,
                broker_account_id=acct.id,
                parent_order_id=trader_order.id,
                instrument_type=trader_order.instrument_type,
                symbol=trader_order.symbol,
                option_expiry=trader_order.option_expiry,
                option_strike=trader_order.option_strike,
                option_right=trader_order.option_right,
                is_closing=is_closing_effective,
                side=trader_order.side,
                order_type=trader_order.order_type,
                quantity=scaled,
                limit_price=trader_order.limit_price,
                stop_price=trader_order.stop_price,
                # TP/SL handling depends on the subscriber's
                # copy_trader_bracket toggle:
                #   * OFF (default): we leave BOTH absolute prices and BOTH
                #     percents NULL. The subscriber's listener calls
                #     bracket_emulator.emulate_bracket_exits on fill, which
                #     short-circuits when nothing is set — no exits. The
                #     subscriber instead relies on their own per-position
                #     TP/SL % (position_enforcer). Trader manages own exits.
                #   * ON: we stamp the trader's bracket below (after the
                #     row is built) as either a re-anchored percent
                #     (take_profit_pct/stop_loss_pct) or, when there's no
                #     usable anchor, absolute prices. We NEVER send a native
                #     broker bracket for mirrors — the emulator places the
                #     exits uniformly across all brokers when the entry fills.
                # Alpaca native bracket: stamp the trader's exit prices so the
                # entry submits as an OrderClass.BRACKET (see the adapter). All
                # other cases leave these NULL and rely on the emulator / copy-
                # trader-bracket path below.
                take_profit_price=(trader_order.take_profit_price if alpaca_native_bracket else None),
                stop_loss_price=(trader_order.stop_loss_price if alpaca_native_bracket else None),
                status=OrderStatus.PENDING,
                subscriber_picked_at=subscriber_picked_at,
                subscriber_accepted_at=subscriber_accepted_at,
            )

            # Copy the trader's bracket onto this mirror when the subscriber
            # opted in AND the trader actually set one. Skipped for an Alpaca
            # native bracket — the entry already carries the real exit prices.
            if sub.copy_trader_bracket and not alpaca_native_bracket:
                if copy_use_pct:
                    child.take_profit_pct = copy_tp_val
                    child.stop_loss_pct = copy_sl_val
                else:
                    child.take_profit_price = copy_tp_val
                    child.stop_loss_price = copy_sl_val
            db.add(child)
            # NOTE: no per-child db.flush() here. Order.id has a Python-side
            # default=uuid.uuid4 (see models/order.py), so child.id is
            # already populated. We can keep referencing it below without
            # a round-trip to Postgres. The single db.flush() at the end
            # of Phase 1 will commit all ~91 child INSERTs in one trip
            # instead of 91.

            try:
                # Need a real BrokerAccount-like object for adapter_for. The
                # cache DTO has the same .broker attribute it needs.
                sub_creds = cache.decrypt_creds_cached(acct.id, acct.encrypted_credentials)
                sub_adapter = adapter_for(acct, sub_creds)
            except Exception as exc:  # noqa: BLE001
                child.status = OrderStatus.REJECTED
                child.reject_reason = f"credentials_error: {exc}"[:480]
                child.closed_at = datetime.now(timezone.utc)
                results.append(FanoutResult(
                    subscriber_user_id=sub.user_id,
                    broker_account_id=acct.id,
                    order_id=child.id,
                    status="error",
                    detail=str(exc)[:200],
                ))
                continue

            # TP/SL are TRADER-ONLY (see the child Order construction
            # above). Hard-code None on the broker request so the
            # subscriber's broker never opens a native bracket either —
            # not even on Alpaca stocks. Subscribers receive plain
            # entries; the trader manages their own exits.
            pending.append(_PendingMirror(
                child_order_id=child.id,
                subscriber_user_id=sub.user_id,
                broker_account_id=acct.id,
                broker=acct.broker,
                adapter=sub_adapter,
                request=BrokerOrderRequest(
                    instrument_type=child.instrument_type,
                    symbol=child.symbol,
                    side=child.side,
                    order_type=child.order_type,
                    quantity=child.quantity,
                    limit_price=child.limit_price,
                    stop_price=child.stop_price,
                    # Native bracket ONLY for the Alpaca-stock case — this is the
                    # single place a mirror is allowed to send a broker-native
                    # bracket. Everyone else stays None (emulator handles exits).
                    take_profit_price=(trader_order.take_profit_price if alpaca_native_bracket else None),
                    stop_loss_price=(trader_order.stop_loss_price if alpaca_native_bracket else None),
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    is_closing=child.is_closing,
                    client_order_id=str(child.id),
                ),
                # Force-fill a close only when the trader has ALREADY filled.
                # A working trader order (mirrored via bring_open_orders) stays a
                # cancellable limit until it fills; Part B forces it then.
                trader_filled=(trader_order.status == OrderStatus.FILLED),
                # The trader's own fill price — anchors an extended-hours limit to
                # where the trader actually traded (see _ext_hours_limit_price).
                trader_fill_price=trader_order.filled_avg_price,
            ))

    # End of Phase 1: one batched flush for every child we just added.
    # Without this we'd have called db.flush() inside the per-account loop
    # ~91 times (one round-trip each). One flush, one round-trip, all
    # INSERTs go to Postgres as a single transactional batch.
    if pending:
        db.flush()

    # ── Phase 2: fire all broker calls in parallel via asyncio ────────────
    # _place_one returns the actual exception object (not just its string)
    # so Phase 3 can call classify_error on it for retry routing. The string
    # form is still used downstream as reject_reason — we just str() it
    # there instead of here.
    async def _place_one(item: _PendingMirror) -> tuple[_PendingMirror, BrokerOrderResult | None, BaseException | None, int]:
        sem = _broker_sem(item.broker)
        async with sem:
            # Time the broker REST call itself — request → response — for BOTH
            # success and error, so the Performance page can surface the raw
            # broker round-trip ("Broker Response" / broker_call_ms).
            start = time.perf_counter()
            last_exc: BaseException | None = None
            for attempt in range(_RATE_LIMIT_ATTEMPTS):
                try:
                    # to_thread keeps the event loop free while the sync SDK does
                    # I/O. For a CLOSE this also auto-resolves order-conflict
                    # rejections (wash trade / uncovered / insufficient qty) by
                    # cancelling the blocking order(s) on the subscriber's account
                    # and retrying — mirroring the direct-close behaviour in
                    # api.trades.
                    resp = await asyncio.to_thread(_place_mirror_with_conflict_resolve, item)
                    return item, resp, None, int((time.perf_counter() - start) * 1000)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    # A broker THROTTLE (SnapTrade 429) means nothing was placed —
                    # wait out the ~1s throttle and retry inline (holding the sem,
                    # which also eases pressure on the broker). ALWAYS retry these,
                    # independent of the subscriber's retry setting. Any other error
                    # falls through to the normal reject/retry-routing below.
                    if is_rate_limit_error(exc) and attempt < _RATE_LIMIT_ATTEMPTS - 1:
                        await asyncio.sleep(_RATE_LIMIT_BACKOFF_S * (attempt + 1))
                        continue
                    return item, None, exc, int((time.perf_counter() - start) * 1000)
            return item, None, last_exc, int((time.perf_counter() - start) * 1000)

    broker_results: list[tuple[_PendingMirror, BrokerOrderResult | None, BaseException | None, int]]
    if pending:
        broker_results = await asyncio.gather(
            *(_place_one(p) for p in pending), return_exceptions=False
        )
    else:
        broker_results = []

    # ── Phase 3: apply results, audit, publish events ──────────────────────
    for item, resp, exc, call_ms in broker_results:
        err = str(exc)[:480] if exc is not None else None
        child = db.get(Order, item.child_order_id)
        child.broker_call_ms = call_ms
        if resp is not None:
            # The place path may have rewritten the order to make it fill (qty
            # rounded for non-fractionable assets; a CLOSE forced to market /
            # marketable-limit). Keep the row in sync with what was ACTUALLY
            # placed so Order History and the TP/SL columns are accurate.
            if item.request.quantity != child.quantity:
                child.quantity = item.request.quantity
            if item.request.order_type != child.order_type:
                child.order_type = item.request.order_type
            child.limit_price = item.request.limit_price
            child.stop_price = item.request.stop_price
            child.status = resp.status
            child.broker_order_id = resp.broker_order_id
            child.submitted_at = resp.submitted_at
            # Lifecycle: the subscriber's broker accepted the child order.
            # Prefer the broker's own timestamp when supplied; fall back to
            # 'now' so the field is never NULL on a successful submit.
            child.broker_accepted_at = resp.submitted_at or datetime.now(timezone.utc)
            child.filled_quantity = resp.filled_quantity
            child.filled_avg_price = resp.filled_avg_price
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.submitted",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "broker_order_id": resp.broker_order_id,
                    "scaled_qty": str(child.quantity),
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="submitted",
            ))
            # Lifecycle: stamp broadcast moment before publishing.
            child.redis_published_at = datetime.now(timezone.utc)
            events.publish(item.subscriber_user_id, _order_event("order.copy_submitted", child))

            # Native bracket: Alpaca returns the TP/SL child legs it created
            # alongside the entry. Materialise them as visible mirror rows so the
            # subscriber sees 1 buy + 2 sells like the trader. They're linked to
            # the entry mirror (bracket_parent_id) and carry the leg's own broker
            # order id, so the reconciler updates them when they arm/fill.
            for leg in resp.bracket_legs:
                leg_row = Order(
                    id=uuid.uuid4(),
                    user_id=item.subscriber_user_id,
                    broker_account_id=item.broker_account_id,
                    parent_order_id=trader_order.id,
                    bracket_parent_id=child.id,
                    instrument_type=child.instrument_type,
                    symbol=child.symbol,
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    is_closing=True,
                    side=leg.side,
                    order_type=leg.order_type,
                    quantity=child.quantity,
                    limit_price=leg.limit_price,
                    stop_price=leg.stop_price,
                    status=leg.status,
                    broker_order_id=leg.broker_order_id,
                    submitted_at=resp.submitted_at,
                    broker_accepted_at=resp.submitted_at or datetime.now(timezone.utc),
                    subscriber_picked_at=child.subscriber_picked_at,
                    subscriber_accepted_at=child.subscriber_accepted_at,
                    redis_published_at=datetime.now(timezone.utc),
                )
                db.add(leg_row)
                db.flush()
                events.publish(item.subscriber_user_id, _order_event("order.copy_submitted", leg_row))
        elif isinstance(exc, _DanglingEntryCancelled):
            # The trader closed before the subscriber's entry filled. We already
            # cancelled the dangling working entry at the broker; record THIS
            # mirror (the would-be sell) as CANCELED so the subscriber ends flat,
            # not stuck with a rejected sell + a live buy. No retry.
            child.status = OrderStatus.CANCELED
            child.reject_reason = (
                "Trader closed before your entry filled — the unfilled entry "
                "was cancelled, so there was nothing to sell."
            )[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.close_skipped_entry_cancelled",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "symbol": child.symbol,
                    "cancelled_entry_ids": [str(i) for i in exc.cancelled_ids],
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="cancelled_unfilled_entry",
            ))
            child.redis_published_at = datetime.now(timezone.utc)
            events.publish(item.subscriber_user_id, _order_event("order.cancelled", child))
        elif isinstance(exc, _OpeningShortSkipped):
            # Trader's SELL reached a subscriber holding nothing to close → placing
            # it would open a NAKED SHORT. We declined (copy_allow_opening_shorts
            # off). Report skipped with a clear reason; the subscriber stays flat.
            child.status = OrderStatus.REJECTED
            child.reject_reason = (
                "Skipped: this sell would open a short position — you hold no "
                "matching position to close (likely a missed/unsynced entry). "
                "Not mirrored to avoid an unintended short."
            )[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.skipped_opening_short",
                entity_type="order",
                entity_id=child.id,
                metadata={"parent_order_id": str(trader_order.id), "symbol": child.symbol},
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="skipped_opening_short",
            ))
            child.redis_published_at = datetime.now(timezone.utc)
            events.publish(item.subscriber_user_id, _order_event("order.copy_failed", child))
        elif isinstance(exc, _DeferUntilEntryFills):
            # The close can't be placed yet — the subscriber's ENTRY is still
            # working (pre-market queue / fast scalp). DON'T reject it. Park it as
            # RETRY_PENDING with NO broker order; fire_deferred_closes_for_entry
            # places it the moment the entry fills. retry_at is a far-out safety
            # net so the retry-scheduler eventually cleans it up if that fill event
            # is ever missed (it will then reject if there's still no position).
            child.status = OrderStatus.RETRY_PENDING
            child.broker_order_id = None
            child.retry_at = datetime.now(timezone.utc) + timedelta(hours=_DEFERRED_CLOSE_TTL_HOURS)
            child.reject_reason = (
                "Waiting for your entry to fill before this close can be placed "
                "(e.g. a pre-market order queued for the open)."
            )[:480]
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.close_deferred_until_entry_fills",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "symbol": child.symbol,
                    "waiting_on_entry_ids": [str(i) for i in exc.entry_ids],
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="deferred_until_entry_fills",
            ))
            child.redis_published_at = datetime.now(timezone.utc)
            events.publish(item.subscriber_user_id, _order_event("order.placed", child))
        elif isinstance(exc, _KeptProtectiveStop):
            # A take-profit LIMIT collided with the subscriber's working stop-loss on
            # the same position (Alpaca backs only ONE resting sell per position). We
            # KEPT the stop (downside protection) and did NOT place this take-profit;
            # the subscriber still exits when the trader's TP fills (fill-driven
            # close). Record as CANCELED with a clear reason — never a scary reject.
            child.status = OrderStatus.CANCELED
            child.reject_reason = (
                "Kept your stop-loss — your broker allows only one resting exit "
                "order per position, so this take-profit wasn't placed. You'll still "
                "exit when the trader's take-profit fills."
            )[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.close_skipped_kept_stop",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "symbol": child.symbol,
                    "kept_stop_ids": [str(i) for i in exc.kept_stop_ids],
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="skipped_kept_protective_stop",
            ))
            child.redis_published_at = datetime.now(timezone.utc)
            events.publish(item.subscriber_user_id, _order_event("order.cancelled", child))
        else:
            # Broker call failed. Classify the error to decide between:
            #   1. User-fixable (insufficient buying power, after-hours
            #      market order, etc.) → REJECTED with a clean message,
            #      no retry — it'd just fail the same way next time.
            #   2. Transient (5xx, 429, timeout, connection reset) AND
            #      subscriber opted in to retries → RETRY_PENDING, the
            #      retry_scheduler picks it up at retry_at.
            #   3. Anything else → REJECTED with the raw error (pre-retry
            #      behaviour).
            #
            # A close mirror must keep its is_closing flag through the retry so
            # the scheduler consults the subscriber's CLOSE retry interval, and
            # so P&L / position tracking still see it as a close. Pick the
            # interval by the order's own is_closing, matching
            # retry_scheduler._passes_gates (DEF-COPY-001).
            sub_settings = db.get(SubscriberSettings, item.subscriber_user_id)
            interval = (
                (sub_settings.retry_interval_close if child.is_closing
                 else sub_settings.retry_interval_open)
                if sub_settings is not None
                else RetryInterval.NEVER
            )
            cls = classify_error(exc) if exc is not None else None

            if cls is not None and cls.clean_message is not None:
                # User-fixable: present the clean message, no retry.
                child.status = OrderStatus.REJECTED
                child.reject_reason = cls.clean_message[:480]
                child.closed_at = datetime.now(timezone.utc)
                audit.record(
                    db,
                    actor_user_id=item.subscriber_user_id,
                    action="copy.error",
                    entity_type="order",
                    entity_id=child.id,
                    metadata={
                        "parent_order_id": str(trader_order.id),
                        "friendly": cls.clean_message,
                        "raw": err,
                        "classification": "user_fixable",
                    },
                )
                results.append(FanoutResult(
                    subscriber_user_id=item.subscriber_user_id,
                    broker_account_id=item.broker_account_id,
                    order_id=child.id,
                    status="error",
                    detail=cls.clean_message[:200],
                ))
                child.redis_published_at = datetime.now(timezone.utc)
                events.publish(item.subscriber_user_id, _order_event("order.copy_failed", child))

            elif (
                cls is not None
                and cls.transient
                and interval != RetryInterval.NEVER
            ):
                # Transient + subscriber wants retries → schedule one.
                # IMPORTANT: keep lifecycle stamps (subscriber_picked_at,
                # subscriber_accepted_at, broker_accepted_at,
                # redis_published_at) intact. The retry flow continues
                # the same order's lifecycle, not a new one.
                minutes = _RETRY_INTERVAL_MINUTES[interval]
                child.status = OrderStatus.RETRY_PENDING
                child.retry_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                # Preserve child.is_closing — resetting a close mirror to
                # opening broke its retry interval + P&L basis (DEF-COPY-001).
                child.reject_reason = "transient broker error, will retry"
                # Don't set closed_at — order isn't terminal.
                audit.record(
                    db,
                    actor_user_id=item.subscriber_user_id,
                    action="copy.retry_scheduled",
                    entity_type="order",
                    entity_id=child.id,
                    metadata={
                        "parent_order_id": str(trader_order.id),
                        "error": err,
                        "retry_at": child.retry_at.isoformat(),
                        "interval_minutes": minutes,
                    },
                )
                results.append(FanoutResult(
                    subscriber_user_id=item.subscriber_user_id,
                    broker_account_id=item.broker_account_id,
                    order_id=child.id,
                    status="retry_scheduled",
                    detail=err[:200] if err else None,
                ))
                child.redis_published_at = datetime.now(timezone.utc)
                # New event type — frontend's SSE union must accept it.
                events.publish(
                    item.subscriber_user_id,
                    _order_event("order.copy_retry_scheduled", child),
                )

            else:
                # Either unknown error, transient but retries disabled, or no
                # classifier verdict. Store a TIDIED reason — never the raw SDK
                # exception with its HTTP-header dump, and never blank (exc was
                # None) — so the user sees a real sentence. The raw string is
                # preserved verbatim in the audit metadata for debugging.
                clean = clean_broker_error(err)
                child.status = OrderStatus.REJECTED
                child.reject_reason = clean[:480]
                child.closed_at = datetime.now(timezone.utc)
                audit.record(
                    db,
                    actor_user_id=item.subscriber_user_id,
                    action="copy.error",
                    entity_type="order",
                    entity_id=child.id,
                    metadata={"parent_order_id": str(trader_order.id), "error": err},
                )
                results.append(FanoutResult(
                    subscriber_user_id=item.subscriber_user_id,
                    broker_account_id=item.broker_account_id,
                    order_id=child.id,
                    status="error",
                    detail=clean[:200],
                ))
                child.redis_published_at = datetime.now(timezone.utc)
                events.publish(item.subscriber_user_id, _order_event("order.copy_failed", child))

    # Notify each subscriber whose mirror was REJECTED — in-app + SMS for
    # opted-in users (create_notification fans out to Twilio off-thread).
    # status == "error" is a FINAL rejection; retries are notified separately
    # only once all attempts are exhausted (retry_scheduler), so no double-send.
    if any(r.status == "error" for r in results):
        from app.services.notifications import create_notification  # noqa: PLC0415
        _side = trader_order.side.value.upper()
        for r in results:
            if r.status != "error":
                continue
            try:
                create_notification(
                    db,
                    user_id=r.subscriber_user_id,
                    type="copy.rejected",
                    message=(
                        f"Your copy of the {_side} {trader_order.symbol} order was "
                        f"rejected: {(r.detail or 'unknown error')[:180]}"
                    ),
                    metadata={
                        "parent_order_id": str(trader_order.id),
                        "order_id": str(r.order_id),
                        "symbol": trader_order.symbol,
                        "side": _side,
                        "reason": (r.detail or "")[:300],
                        "trader_id": str(trader_order.user_id),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("copy: rejection notification failed for order %s", r.order_id)

    return results


# ── Sync wrapper kept for callers that haven't been awaited yet ──────────


def fanout(db: Session, trader_order: Order, trader: User) -> list[FanoutResult]:
    """Sync entrypoint. Runs the async fanout in a fresh event loop. Prefer
    calling fanout_async directly from async contexts."""
    return asyncio.run(fanout_async(db, trader_order, trader))


def fanout_threadsafe(
    order_id: uuid.UUID,
    trader_id: uuid.UUID,
    loop: asyncio.AbstractEventLoop,
) -> list[FanoutResult]:
    """Fan out an already-persisted trader order from a listener worker
    thread, running the async fanout on the app's MAIN event loop.

    Why not the sync ``fanout`` here: ``fanout`` does ``asyncio.run`` which
    creates a throwaway loop per order. The per-broker ``asyncio.Semaphore``
    cache (and the async Redis client, keyed by loop id) bind to whatever
    loop first touched them, so a second listener-detected order on a fresh
    throwaway loop raises ``Semaphore is bound to a different event loop``
    and the mirror silently fails. Dispatching onto the single long-lived
    main loop keeps every order on the same loop.

    Opens its OWN DB session on the loop thread — never shares the caller's
    worker-thread Session across threads (SQLAlchemy Sessions aren't
    thread-safe). The trader order must already be committed; we re-load it
    by id. Marks it fanned-out and commits. Blocks until the fanout finishes.
    """
    async def _run() -> list[FanoutResult]:
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            trader = db.get(User, trader_id)
            if order is None or trader is None:
                return []
            results = await fanout_async(db, order, trader)
            # Only flag as broadcast if copy was actually ACTIVE. When the
            # trader's master copy is paused, fanout_async no-ops (returns
            # early) and nothing was sent to subscribers — so leave the flag
            # False. Otherwise an order placed (or observed) while copy was
            # OFF would wrongly land in the trader's "All Orders" tab
            # (copy-on) instead of "My Orders" (copy-off).
            ts = db.get(TraderSettings, trader_id)
            if not (ts is not None and ts.copy_paused):
                order.fanned_out_to_subscribers = True
            db.commit()
            return results

    return asyncio.run_coroutine_threadsafe(_run(), loop).result()


def _order_event(event_type: str, order: Order) -> dict[str, Any]:
    """Compact payload — frontend can use it directly to prepend a row."""
    return {
        "type": event_type,
        "order": {
            "id": str(order.id),
            "parent_order_id": str(order.parent_order_id) if order.parent_order_id else None,
            "broker_account_id": str(order.broker_account_id),
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": str(order.quantity),
            # Order TERMS — carried so the frontend reflects a broker-side
            # MODIFY (new limit/stop/qty/type) instantly on the SSE frame,
            # instead of waiting for the ~1.5s reconcile refetch.
            "limit_price": str(order.limit_price) if order.limit_price is not None else None,
            "stop_price": str(order.stop_price) if order.stop_price is not None else None,
            "filled_quantity": str(order.filled_quantity or 0),
            "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            "status": order.status.value,
            "broker_order_id": order.broker_order_id,
            "instrument_type": order.instrument_type.value,
            # Option fields — let the Order History Call/Put + Expiry columns
            # render immediately for a freshly-arrived option order.
            "option_expiry": order.option_expiry.isoformat() if order.option_expiry else None,
            "option_strike": str(order.option_strike) if order.option_strike is not None else None,
            "option_right": order.option_right.value if order.option_right else None,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "reject_reason": order.reject_reason,
        },
    }
