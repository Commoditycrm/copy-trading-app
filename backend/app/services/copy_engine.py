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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, BrokerOrderResult, adapter_for
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus
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


# Statuses whose UNFILLED remainder still reserves shares at the broker
# (the broker's "held_for_orders"). A second close of the same shares while
# one of these is working gets rejected (e.g. Alpaca 40310000).
_WORKING_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _closeable_quantity(db: Session, user_id: uuid.UUID, order: Order) -> Decimal:
    """Quantity the subscriber can still CLOSE in ``order``'s direction: their
    net filled position for the contract, MINUS what their own still-working
    orders on the same side have already reserved at the broker.

    Two things reduce what a new close can take:
      * net filled position (filled buys − sells) — what they actually hold;
      * unfilled qty on open same-side orders — the broker's ``held_for_orders``.
        Without subtracting this, a second close of shares a prior working close
        already reserved rejects with "insufficient qty available".

    Tracks reality as fills sync in (SnapTrade reconciler + fills_sync). Returns
    a non-negative quantity."""
    same_contract = (
        Order.user_id == user_id,
        Order.symbol == order.symbol,
        Order.instrument_type == order.instrument_type,
        Order.option_expiry.is_not_distinct_from(order.option_expiry),
        Order.option_strike.is_not_distinct_from(order.option_strike),
        Order.option_right.is_not_distinct_from(order.option_right),
    )
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
    reserved = db.execute(
        select(
            func.coalesce(
                func.sum(Order.quantity - func.coalesce(Order.filled_quantity, 0)), 0
            )
        ).where(
            *same_contract,
            Order.side == order.side,
            Order.status.in_(_WORKING_ORDER_STATUSES),
        )
    ).scalar_one()

    closeable = net_in_direction - Decimal(str(reserved))
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
                closeable = _closeable_quantity(db, child.user_id, trader_order)
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
            results = list(pool.map(_replace, pending))

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
                closeable = _closeable_quantity(db, child.user_id, new_order)
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

        # Phase 2 (thread pool): cancel the old mirror, then place the new one.
        def _cancel_then_place(item: tuple[Order, Any, BrokerOrderRequest, uuid.UUID]):
            old_ch, ad, rq, new_id = item
            try:
                ad.cancel_order(old_ch.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                return old_ch.id, new_id, None, f"cancel_failed: {exc}"[:300]
            try:
                return old_ch.id, new_id, ad.place_order(rq), None
            except Exception as exc:  # noqa: BLE001
                return old_ch.id, new_id, None, f"place_failed: {exc}"[:300]

        with ThreadPoolExecutor(max_workers=min(32, len(plan))) as pool:
            results = list(pool.map(_cancel_then_place, plan))

        # Phase 3 (session thread): apply.
        req_by_new_id = {new_id: rq for _c, _a, rq, new_id in plan}
        for old_id, new_id, resp, err in results:
            old_ch = db.get(Order, old_id)
            if old_ch is None:
                continue
            rq = req_by_new_id[new_id]
            if err is not None and err.startswith("cancel_failed"):
                # Old order still live (likely mid-fill) — leave it, place nothing.
                audit.record(
                    db, actor_user_id=old_ch.user_id, action="order.mirror_modify_cancel_failed",
                    entity_type="order", entity_id=old_ch.id,
                    metadata={"parent_order_id": str(old_trader_order_id), "broker_order_id": old_ch.broker_order_id, "error": err},
                )
                continue
            # Cancel succeeded — the old mirror is gone at the broker.
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

    # Trader master pause — skip all fanout when set.
    ts = db.get(TraderSettings, trader.id)
    if ts is not None and ts.copy_paused:
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

    for sub in subs:
        # Lifecycle: the moment the engine picks this subscriber up for
        # processing. Applied to every child Order created in this iteration
        # below. Captured here (not inside the inner per-account loop) so it
        # reflects the per-subscriber pick, not per-account. After batching,
        # all picked_at values are within microseconds — pick_lag is now a
        # platform-overhead floor, not a queue-position artifact.
        subscriber_picked_at = datetime.now(timezone.utc)

        # Daily P&L kill switches (check BEFORE placing). Loss + profit
        # share the same auto-pause path — both stamp pnl_auto_paused_at
        # as an audit marker. Re-enable is MANUAL ONLY (Settings UI).
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
            # Defense-in-depth: never mirror a CLOSE larger than the subscriber
            # actually holds. A position desync (e.g. an earlier open that never
            # filled on their account, or a multiplier that put them out of step
            # with the trader) would otherwise make the broker reject the whole
            # close with "No matching position to close". Clamp to their net
            # filled position for this contract so the close flattens what they
            # hold and no more. is_closing is set by the broker listeners (e.g.
            # SnapTrade's SELL_TO_CLOSE); non-close orders are never touched.
            if trader_order.is_closing and scaled > 0:
                closeable = _closeable_quantity(db, sub.user_id, trader_order)
                if closeable < scaled:
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
                # Multi-channel alert (in-app + email/SMS per prefs). Only the
                # user-fixable branch notifies — systemic errors (credential
                # decrypt, transient) are intentionally left to in-app SSE to
                # avoid emailing every subscriber during a fanout-wide failure.
                try:
                    from app.services import notifications as _notifications  # noqa: PLC0415
                    _notifications.notify_order_event(db, child, "order.rejected")
                except Exception:  # noqa: BLE001
                    log.exception("copy_engine: reject notify failed for %s", child.id)

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
