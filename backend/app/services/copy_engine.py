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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, BrokerOrderResult, adapter_for
from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderStatus
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services import audit, cache, events
from app.services.crypto import decrypt_json
from app.services.order_retry import classify_error
from app.services.pnl import today_realized_pnl


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

    # Trader master pause — skip all fanout when set.
    ts = db.get(TraderSettings, trader.id)
    if ts is not None and ts.copy_paused:
        return results

    # ── Phase 1: build child orders + skip records ─────────────────────────
    subs = await cache.get_subscribers_for_trader(db, trader.id)

    for sub in subs:
        # Lifecycle: the moment the engine picks this subscriber up for
        # processing. Applied to every child Order created in this iteration
        # below. Captured here (not inside the inner per-account loop) so it
        # reflects the per-subscriber pick, not per-account.
        subscriber_picked_at = datetime.now(timezone.utc)

        sub_user = db.get(User, sub.user_id)
        if not sub_user:
            continue

        # Daily-loss kill switch (check BEFORE placing).
        if sub.daily_loss_limit is not None:
            todays_pnl = today_realized_pnl(db, sub.user_id)
            if todays_pnl <= -sub.daily_loss_limit:
                # Flip the DB row off so future fanouts skip cheap. Also bust
                # the subscriber cache so other workers see it on next read.
                db_settings = db.get(SubscriberSettings, sub.user_id)
                if db_settings is not None:
                    db_settings.copy_enabled = False
                cache.invalidate_subscribers_for_trader(trader.id)
                audit.record(
                    db,
                    actor_user_id=sub.user_id,
                    action="copy.auto_paused_daily_loss",
                    entity_type="subscriber_settings",
                    entity_id=sub.user_id,
                    metadata={
                        "daily_loss_limit": str(sub.daily_loss_limit),
                        "todays_realized_pnl": str(todays_pnl),
                        "trigger_order_id": str(trader_order.id),
                    },
                )
                events.publish(sub.user_id, {
                    "type": "copy.auto_paused",
                    "reason": "daily_loss_limit",
                    "daily_loss_limit": str(sub.daily_loss_limit),
                    "todays_realized_pnl": str(todays_pnl),
                })
                results.append(FanoutResult(
                    subscriber_user_id=sub.user_id,
                    broker_account_id=uuid.UUID(int=0),
                    order_id=None,
                    status="skipped_daily_loss_limit",
                ))
                continue

        sub_accounts = await cache.get_broker_accounts(db, sub.user_id)
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
                user_id=sub.user_id,
                broker_account_id=acct.id,
                parent_order_id=trader_order.id,
                instrument_type=trader_order.instrument_type,
                symbol=trader_order.symbol,
                option_expiry=trader_order.option_expiry,
                option_strike=trader_order.option_strike,
                option_right=trader_order.option_right,
                side=trader_order.side,
                order_type=trader_order.order_type,
                quantity=scaled,
                limit_price=trader_order.limit_price,
                stop_price=trader_order.stop_price,
                status=OrderStatus.PENDING,
                subscriber_picked_at=subscriber_picked_at,
                subscriber_accepted_at=subscriber_accepted_at,
            )
            db.add(child)
            db.flush()

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
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    client_order_id=str(child.id),
                ),
            ))

    # ── Phase 2: fire all broker calls in parallel via asyncio ────────────
    # _place_one returns the actual exception object (not just its string)
    # so Phase 3 can call classify_error on it for retry routing. The string
    # form is still used downstream as reject_reason — we just str() it
    # there instead of here.
    async def _place_one(item: _PendingMirror) -> tuple[_PendingMirror, BrokerOrderResult | None, BaseException | None]:
        sem = _broker_sem(item.broker)
        async with sem:
            try:
                # to_thread keeps the event loop free while the sync SDK does I/O.
                resp = await asyncio.to_thread(item.adapter.place_order, item.request)
                return item, resp, None
            except Exception as exc:  # noqa: BLE001
                return item, None, exc

    broker_results: list[tuple[_PendingMirror, BrokerOrderResult | None, BaseException | None]]
    if pending:
        broker_results = await asyncio.gather(
            *(_place_one(p) for p in pending), return_exceptions=False
        )
    else:
        broker_results = []

    # ── Phase 3: apply results, audit, publish events ──────────────────────
    for item, resp, exc in broker_results:
        err = str(exc)[:480] if exc is not None else None
        child = db.get(Order, item.child_order_id)
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
