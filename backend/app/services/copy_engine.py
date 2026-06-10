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
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, BrokerOrderResult, adapter_for
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderStatus
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services import audit, cache, events
from app.services.platform_config import get_fanout_batch_threshold_async
from app.services.crypto import decrypt_json
from app.services.order_retry import classify_error
from app.services.pnl import today_realized_pnl, today_realized_pnl_bulk


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


def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    raw = trader_qty * multiplier
    if fractional:
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw.to_integral_value(rounding=ROUND_DOWN)


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

    # Trader master pause — skip all fanout when set.
    ts = db.get(TraderSettings, trader.id)
    if ts is not None and ts.copy_paused:
        return results

    # ── Phase 1: build child orders + skip records ─────────────────────────
    subs = await cache.get_subscribers_for_trader(db, trader.id)

    # ── Daily auto-resume sweep ────────────────────────────────────────────
    # For every subscriber whose copy was previously auto-paused by a P&L
    # limit, check whether the pause was set on a PRIOR UTC day. If so,
    # clear the pause + re-enable copy_enabled so today's trades flow.
    # The pause is keyed off pnl_auto_paused_at (not just copy_enabled=False)
    # so we don't re-enable users who manually disabled copy.
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
                    metadata={"paused_at": paused_iso, "resumed_at": today_utc.isoformat()},
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

    for sub in subs:
        # Lifecycle: the moment the engine picks this subscriber up for
        # processing. Applied to every child Order created in this iteration
        # below. Captured here (not inside the inner per-account loop) so it
        # reflects the per-subscriber pick, not per-account. After batching,
        # all picked_at values are within microseconds — pick_lag is now a
        # platform-overhead floor, not a queue-position artifact.
        subscriber_picked_at = datetime.now(timezone.utc)

        # Daily P&L kill switches (check BEFORE placing). Loss + profit
        # share the same auto-pause + auto-resume machinery — both stamp
        # pnl_auto_paused_at so the next-day sweep above re-enables them.
        if sub.daily_loss_limit is not None or sub.daily_profit_limit is not None:
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
                # Flip the DB row off + stamp pnl_auto_paused_at so the
                # next-day sweep above re-enables this subscriber on the
                # next fanout after UTC midnight.
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
        trade_symbol = (trader_order.symbol or "").upper()
        excl = sub.symbol_exclusion_list or ()
        incl = sub.symbol_inclusion_list or ()
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
        sub_accounts = (
            accts_by_user.get(sub.user_id, [])
            if use_batch
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
            scaled = _scale_quantity(
                trader_order.quantity, sub.multiplier, acct.supports_fractional
            )
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
                is_closing=trader_order.is_closing,
                side=trader_order.side,
                order_type=trader_order.order_type,
                quantity=scaled,
                limit_price=trader_order.limit_price,
                stop_price=trader_order.stop_price,
                # TP/SL are TRADER-ONLY. We deliberately do NOT propagate
                # the trader's take_profit_price / stop_loss_price onto
                # the subscriber's mirrored entry. Two reasons we leave
                # these NULL:
                #   1. The subscriber's broker should never place a
                #      native bracket for them — see the
                #      BrokerOrderRequest below which now hard-codes
                #      None for both.
                #   2. The subscriber's listener calls
                #      bracket_emulator.emulate_bracket_exits when this
                #      child fills. That function short-circuits when
                #      BOTH prices are NULL ("if not tp_price and not
                #      sl_price: return []"), so no exits are spawned.
                # Net result: subscribers mirror entries only; the
                # trader manages exits on their own account.
                take_profit_price=None,
                stop_loss_price=None,
                status=OrderStatus.PENDING,
                subscriber_picked_at=subscriber_picked_at,
                subscriber_accepted_at=subscriber_accepted_at,
            )
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
                    take_profit_price=None,
                    stop_loss_price=None,
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    is_closing=child.is_closing,
                    client_order_id=str(child.id),
                ),
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
            try:
                # to_thread keeps the event loop free while the sync SDK does I/O.
                resp = await asyncio.to_thread(item.adapter.place_order, item.request)
                return item, resp, None, int((time.perf_counter() - start) * 1000)
            except Exception as exc:  # noqa: BLE001
                return item, None, exc, int((time.perf_counter() - start) * 1000)

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
            # TODO(is_closing): detecting open-vs-close requires position-
            # aware logic this branch doesn't have yet. Always treat as
            # opening for now (`is_closing=False`, retry_interval_open is
            # the only knob consulted). Closing-detection is a follow-up.
            sub_settings = db.get(SubscriberSettings, item.subscriber_user_id)
            interval = (
                sub_settings.retry_interval_open
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
                child.is_closing = False  # TODO: close-detection
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
                # Either unknown error, transient but retries disabled,
                # or no classifier verdict. Fall back to original behaviour.
                child.status = OrderStatus.REJECTED
                child.reject_reason = err
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
                    detail=err[:200] if err else None,
                ))
                child.redis_published_at = datetime.now(timezone.utc)
                events.publish(item.subscriber_user_id, _order_event("order.copy_failed", child))

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
            "filled_quantity": str(order.filled_quantity or 0),
            "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            "status": order.status.value,
            "broker_order_id": order.broker_order_id,
            "instrument_type": order.instrument_type.value,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "reject_reason": order.reject_reason,
        },
    }
