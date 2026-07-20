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
from app.services.order_retry import classify_error, is_order_conflict_error, live_closeable_quantity
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


def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    raw = trader_qty * multiplier
    if fractional:
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw.to_integral_value(rounding=ROUND_DOWN)


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


# Statuses whose UNFILLED remainder still reserves shares at the broker
# (the broker's "held_for_orders"). A second close of the same shares while
# one of these is working gets rejected (e.g. Alpaca 40310000).
_WORKING_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


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


def _to_immediate_close(adapter: Any, req: BrokerOrderRequest) -> BrokerOrderRequest:
    """Rewrite a CLOSE order so it fills IMMEDIATELY — so a subscriber always
    exits when the trader does. A copied LIMIT close routinely rests unfilled
    (the price moves during copy latency), leaving the subscriber stuck in a
    position the trader already left. This forces the exit:

      * STOCK  → MARKET order (fills at the current market).
      * OPTION → a MARKETABLE LIMIT at the current bid (SELL) / ask (BUY).
        Alpaca rejects option MARKET orders ("no available quote", 40310000),
        so we price a limit through the market instead — it fills like a market
        order. If the quote can't be read, we leave the order unchanged (no
        worse than today) rather than guess a bad price.

    Only ever called for closes (is_closing=True); entries are never touched.
    """
    if req.instrument_type == InstrumentType.STOCK:
        return replace(req, order_type=OrderType.MARKET, limit_price=None, stop_price=None)

    # ── OPTION: marketable limit ──
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
        if (
            not should_close_now
            and req.side == OrderSide.SELL
            and req.instrument_type in (InstrumentType.STOCK, InstrumentType.OPTION)
        ):
            held = live_closeable_quantity(item.adapter, req)
            if held is not None and held > 0:
                should_close_now = True
            elif held is not None and held == 0:
                # Trader is selling to close but we see NO position yet — the
                # subscriber's entry BUY is still working (a RACE: the trader's
                # buy→sell was fast enough that the close arrives while our BUY
                # hasn't filled). A naked SELL here just rejects. Cancel the
                # working entry so it can't strand the subscriber, then re-check
                # the LIVE position a few times — if a fill landed anyway,
                # market-close it; only if truly flat do we report the entry
                # cancelled. (Nothing to cancel → fall through: may be a genuine
                # opening short, which we mirror.)
                cancelled = _cancel_subscriber_conflicts(item)
                cancelled_working_entry = bool(cancelled)
                if cancelled:
                    for _ in range(3):
                        recheck = live_closeable_quantity(item.adapter, req)
                        if recheck is not None and recheck > 0:
                            should_close_now = True  # a fill snuck in → close it
                            break
                        time.sleep(1.0)
                    if not should_close_now:
                        raise _DanglingEntryCancelled(cancelled)
        if should_close_now:
            req = _to_immediate_close(item.adapter, req)
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
            req = _to_immediate_close(item.adapter, req)
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
        if not (req.is_closing and is_order_conflict_error(exc)):
            raise

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
) -> Decimal:
    """Quantity the subscriber can still CLOSE in ``order``'s direction: their
    net filled position for the contract, MINUS what their own still-working
    orders on the same side have already reserved at the broker.

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
                cancelled = ad.cancel_order(old_ch.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                return old_ch.id, new_id, None, f"cancel_failed: {exc}"[:300]
            # A False here means the broker had nothing to cancel — the order is
            # already terminal, and the overwhelmingly likely reason is that it
            # FILLED. Placing the replacement now would double the position, so
            # bail. This is not hypothetical: prod doubled a subscriber's META
            # entry exactly this way. SnapTrade returns 1070 ("failed to cancel")
            # while its own order feed still shows the mirror as working — its
            # data lagged the real fill by ~42s — so neither our DB nor a
            # get_order re-check could see the truth. The cancel result is the
            # only signal that reflects the broker's ACTUAL state at this moment.
            if cancelled is False:
                return old_ch.id, new_id, None, "cancel_noop_already_terminal"
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
            req = _to_immediate_close(adapter, req)
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
        def _cancel_then_place(item: tuple[Order, Any, BrokerOrderRequest, uuid.UUID]):
            old_ch, ad, rq, new_id = item
            try:
                cancelled = ad.cancel_order(old_ch.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                return old_ch.id, new_id, None, f"cancel_failed: {exc}"[:300]
            # A False here means the broker had nothing to cancel — the order is
            # already terminal, and the overwhelmingly likely reason is that it
            # FILLED. Placing the replacement now would double the position, so
            # bail. This is not hypothetical: prod doubled a subscriber's META
            # entry exactly this way. SnapTrade returns 1070 ("failed to cancel")
            # while its own order feed still shows the mirror as working — its
            # data lagged the real fill by ~42s — so neither our DB nor a
            # get_order re-check could see the truth. The cancel result is the
            # only signal that reflects the broker's ACTUAL state at this moment.
            if cancelled is False:
                return old_ch.id, new_id, None, "cancel_noop_already_terminal"
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


def fanout_suppressed(ts: "TraderSettings | None") -> bool:
    """True when the trader has switched copying off, so a detected order must
    NOT be mirrored to subscribers.

    Honours BOTH trader switches, which is the fix for the reported bug:
      * copy_paused      — the top-nav "master fanout gate".
      * trading_enabled  — the Settings-page "Trading — Turn OFF" button.

    Before this, fanout gated only on copy_paused, so a trader who turned
    "Trading OFF" (trading_enabled=false) and then placed an order at their
    broker still had it copied — the listener path never consulted
    trading_enabled (only the in-app placement path did, via trader_can_trade).

    A missing settings row (ts is None) is treated as NOT suppressed, matching
    the prior copy_paused-only behaviour — the row is created at registration
    with trading_enabled=True, so None is an anomaly we stay permissive on
    rather than silently freezing an existing trader's copying."""
    return ts is not None and (ts.copy_paused or not ts.trading_enabled)


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

    # Trader switched copying off — skip all fanout. Covers BOTH the master
    # fanout pause (copy_paused) AND the Settings "Trading OFF" switch
    # (trading_enabled); see fanout_suppressed for why both matter.
    ts = db.get(TraderSettings, trader.id)
    if fanout_suppressed(ts):
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

    # End-of-day lockout (order-level, so computed once). In the last 15 minutes
    # before the US close we do NOT mirror SAME-DAY-EXPIRY option orders to
    # subscribers — the eod_autoclose sweep is flattening those very contracts at
    # 15:45, so letting a fresh 0DTE mirror through would just re-strand the
    # subscriber (or, for a close, no-op against a position we already flattened).
    # Later-expiry options and all stocks pass through untouched.
    eod_locked = (
        get_settings().eod_autoclose_enabled
        and trader_order.instrument_type == InstrumentType.OPTION
        and market_hours.in_eod_close_window()
        and market_hours.is_same_day_expiry(trader_order.option_expiry)
    )

    for sub in subs:
        # Lifecycle: the moment the engine picks this subscriber up for
        # processing. Applied to every child Order created in this iteration
        # below. Captured here (not inside the inner per-account loop) so it
        # reflects the per-subscriber pick, not per-account. After batching,
        # all picked_at values are within microseconds — pick_lag is now a
        # platform-overhead floor, not a queue-position artifact.
        subscriber_picked_at = datetime.now(timezone.utc)

        # EOD lockout: refuse new same-day-expiry option mirrors in the final 15
        # minutes before the US close (see eod_locked above).
        if eod_locked:
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

            # Defense-in-depth: never mirror a CLOSE larger than the subscriber
            # actually holds. Clamp to the raw held position (not minus reserved):
            # if a working order reserves it, the conflict-resolve retry in
            # _place_mirror_with_conflict_resolve cancels that order and re-places,
            # rather than shrinking the close to zero here.
            if is_closing_effective and scaled > 0:
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
                # For a CLOSE this also auto-resolves order-conflict rejections
                # (wash trade / uncovered / insufficient qty) by cancelling the
                # blocking order(s) on the subscriber's account and retrying —
                # mirroring the direct-close behaviour in api.trades.
                resp = await asyncio.to_thread(_place_mirror_with_conflict_resolve, item)
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
            # trader has copying switched off (master pause OR Trading OFF),
            # fanout_async no-ops (returns early) and nothing was sent to
            # subscribers — so leave the flag False. Otherwise an order placed
            # (or observed) while copy was OFF would wrongly land in the
            # trader's "All Orders" tab (copy-on) instead of "My Orders". Uses
            # the SAME predicate as fanout_async so the two can't disagree.
            ts = db.get(TraderSettings, trader_id)
            if not fanout_suppressed(ts):
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
