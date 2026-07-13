"""Realized P&L calculation from fills.

Per-user, per-symbol, per-instrument FIFO matching. Open lots roll forward.
For options we key on the full contract identity (symbol + expiry + strike + right).
For now we ignore commissions/fees beyond the per-fill `fee` column.

Returns daily realized P&L within [start, end] inclusive, bucketed by the
US market timezone (America/New_York). All US equities & options trade on
that clock, so the day boundary matches what traders perceive as "today's
session" regardless of where they're sitting.
"""
from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Fill, InstrumentType, Order, OrderSide

try:
    _MARKET_TZ = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    # Some minimal Python images ship without tzdata. Fall back to a fixed
    # ET offset (good enough — we only use this for day-bucketing, not for
    # rendering times. EDT is wrong for half the year by 1 hour but never
    # by a whole day, so daily P&L still buckets correctly.)
    from datetime import timedelta as _td

    class _FixedET(timezone):
        def __init__(self):
            super().__init__(_td(hours=-5), name="ET")
    _MARKET_TZ = _FixedET()  # type: ignore[assignment]


@dataclass
class _Lot:
    qty: Decimal
    price: Decimal


def _instrument_key(o: Order) -> tuple:
    if o.instrument_type == InstrumentType.OPTION:
        return (
            "OPT",
            o.symbol,
            o.option_expiry,
            str(o.option_strike),
            o.option_right.value if o.option_right else None,
        )
    return ("STK", o.symbol)


def _tz_or_market(tz_name: str | None) -> "ZoneInfo | timezone":
    """Resolve the bucketing timezone. Falls back to the market timezone if
    the caller didn't supply one or the name is unknown."""
    if not tz_name:
        return _MARKET_TZ
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return _MARKET_TZ


def today_filled_notional(
    db: Session, user_id: uuid.UUID, tz_name: str | None = None,
) -> Decimal:
    """Gross USD value of every fill placed today for ``user_id``.

    Returns ``sum(abs(filled_qty) * filled_avg_price * multiplier)`` across
    every order that has any filled quantity recorded with a transaction
    time in today's market-timezone day. Both BUY and SELL count — this is
    capital deployed, not net P&L. Options pick up the 100x contract
    multiplier so the dollar amount reflects actual exposure, matching
    how an Alpaca/SnapTrade dashboard surfaces it.

    Used by the per-day ``max_account_pct_per_day`` kill switch in
    ``services.pnl_poller``: when today's trading value crosses
    ``equity * pct/100``, copy is auto-paused.
    """
    tz = _tz_or_market(tz_name)
    today = datetime.now(tz).date()

    orders = list(db.execute(
        select(Order).where(
            Order.user_id == user_id,
            Order.filled_quantity > 0,
            Order.filled_avg_price.isnot(None),
        )
    ).scalars())
    if not orders:
        return Decimal(0)

    order_ids = [o.id for o in orders]
    fills_by_order: dict[uuid.UUID, list[Fill]] = defaultdict(list)
    for f in db.execute(
        select(Fill).where(Fill.order_id.in_(order_ids))
    ).scalars():
        fills_by_order[f.order_id].append(f)

    total = Decimal(0)
    for o in orders:
        unit = Decimal(100) if o.instrument_type == InstrumentType.OPTION else Decimal(1)
        fs = fills_by_order.get(o.id)
        if fs:
            for f in fs:
                if f.filled_at.astimezone(tz).date() == today:
                    total += abs(f.quantity) * f.price * unit
        else:
            # No detailed fills synced yet — fall back to the order's
            # aggregate. Mirrors the same fallback ``realized_pnl_by_day``
            # uses so the two numbers are consistent with each other.
            when = o.closed_at or o.submitted_at or o.created_at
            if when is None:
                continue
            if when.astimezone(tz).date() != today:
                continue
            total += abs(o.filled_quantity) * o.filled_avg_price * unit
    return total


def today_realized_pnl(db: Session, user_id: uuid.UUID, tz_name: str | None = None) -> Decimal:
    """Realized P&L for "today" in the chosen timezone. Negative = loss."""
    tz = _tz_or_market(tz_name)
    today = datetime.now(tz).date()
    daily = realized_pnl_by_day(db, user_id, start=today, end=today, tz_name=tz_name)
    pnl, _ = daily.get(today, (Decimal(0), 0))
    return pnl


def today_realized_pnl_bulk(
    db: Session,
    user_ids: list[uuid.UUID],
    tz_name: str | None = None,
) -> dict[uuid.UUID, Decimal]:
    """Batched ``today_realized_pnl`` — one P&L number per user, in two
    queries total instead of 2 per user.

    Used by ``copy_engine.fanout_async`` so a 91-subscriber fanout where
    many have daily-loss-limit set doesn't issue 182 round-trips before
    Phase 2 starts. Users with no fills (or no closing trades today)
    are mapped to ``Decimal(0)``.

    Same FIFO matching as ``realized_pnl_by_day``, just per-user
    partitioned in memory. Caller pays Python CPU once for the lot
    walk, no extra SQL.
    """
    if not user_ids:
        return {}

    bucket_tz = _tz_or_market(tz_name)
    today = datetime.now(bucket_tz).date()

    # Query 1: all orders belonging to any of the requested users that
    # have any fill quantity recorded. .in_() is bounded by SQLite's
    # 999-parameter limit; in practice we never exceed a few hundred
    # subscribers per fanout.
    orders: list[Order] = list(db.execute(
        select(Order).where(
            Order.user_id.in_(user_ids),
            Order.filled_quantity > 0,
            Order.filled_avg_price.isnot(None),
        )
    ).scalars())

    # Default everyone to 0 so missing-from-orders users still appear in result.
    result: dict[uuid.UUID, Decimal] = {uid: Decimal(0) for uid in user_ids}
    if not orders:
        return result

    # Query 2: every Fill row attached to those orders.
    orders_by_user: dict[uuid.UUID, list[Order]] = defaultdict(list)
    for o in orders:
        orders_by_user[o.user_id].append(o)

    order_ids = [o.id for o in orders]
    fills_by_order: dict[uuid.UUID, list[Fill]] = defaultdict(list)
    for f in db.execute(
        select(Fill).where(Fill.order_id.in_(order_ids))
    ).scalars():
        fills_by_order[f.order_id].append(f)

    # Per-user FIFO lot walk. Mirrors realized_pnl_by_day but we only
    # need today's running total — once we pass `today` we can stop
    # walking that user's timeline (history beyond today has no effect
    # on the daily-loss-limit check).
    for uid in user_ids:
        user_orders = orders_by_user.get(uid)
        if not user_orders:
            continue  # already 0

        # Build (when, qty, price, order) timeline.
        timeline: list[tuple[datetime, Decimal, Decimal, Order]] = []
        for o in user_orders:
            fs = fills_by_order.get(o.id)
            if fs:
                for f in fs:
                    timeline.append((f.filled_at, f.quantity, f.price, o))
            else:
                when = o.closed_at or o.submitted_at or o.created_at
                timeline.append((when, o.filled_quantity, o.filled_avg_price, o))
        timeline.sort(key=lambda e: e[0])

        open_lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
        today_pnl = Decimal(0)

        for filled_at, fill_qty, fill_price, order in timeline:
            day = filled_at.astimezone(bucket_tz).date()
            if day > today:
                break  # we don't care about fills after today

            key = _instrument_key(order)
            unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
            qty = fill_qty
            price = fill_price

            if order.side == OrderSide.BUY:
                # Close shorts first (negative lots).
                if open_lots[key] and open_lots[key][0].qty < 0:
                    pnl = Decimal(0)
                    while qty > 0 and open_lots[key] and open_lots[key][0].qty < 0:
                        lot = open_lots[key][0]
                        take = min(qty, -lot.qty)
                        pnl += (lot.price - price) * take * unit
                        lot.qty += take
                        qty -= take
                        if lot.qty == 0:
                            open_lots[key].popleft()
                    if day == today:
                        today_pnl += pnl
                    if qty > 0:
                        open_lots[key].append(_Lot(qty=qty, price=price))
                else:
                    open_lots[key].append(_Lot(qty=qty, price=price))
            else:  # SELL — close longs first
                if open_lots[key] and open_lots[key][0].qty > 0:
                    pnl = Decimal(0)
                    while qty > 0 and open_lots[key] and open_lots[key][0].qty > 0:
                        lot = open_lots[key][0]
                        take = min(qty, lot.qty)
                        pnl += (price - lot.price) * take * unit
                        lot.qty -= take
                        qty -= take
                        if lot.qty == 0:
                            open_lots[key].popleft()
                    if day == today:
                        today_pnl += pnl
                    if qty > 0:
                        open_lots[key].append(_Lot(qty=-qty, price=price))
                else:
                    open_lots[key].append(_Lot(qty=-qty, price=price))

        result[uid] = today_pnl

    return result


def realized_pnl_by_day(
    db: Session,
    user_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    tz_name: str | None = None,
    mirrors_only: bool = False,
) -> dict[date, tuple[Decimal, int]]:
    """Returns {day: (realized_pnl, trade_count)}. trade_count is the number of
    closing fills on that day.

    Source of truth is the `fills` table. For freshly filled orders whose
    detailed Fill rows haven't synced from the broker's activity feed yet,
    we synthesize a single fill from the order's aggregate `filled_quantity`
    + `filled_avg_price` so P&L shows up immediately instead of lagging
    minutes behind the broker.

    mirrors_only: count ONLY copy-mirror orders (parent_order_id set), ignoring
    standalone rows. Set for SUBSCRIBERS — the SnapTrade listener re-records a
    subscriber's Webull mirror fills as duplicate standalone orders, so counting
    both double-counts and scrambles the FIFO. A pure copy-subscriber's real
    trades ARE the mirrors, so this de-duplicates them.
    """
    # Orders the user owns with any fill recorded. For subscribers we take
    # mirrors only (see mirrors_only) so listener duplicates don't double-count.
    conds = [
        Order.user_id == user_id,
        Order.filled_quantity > 0,
        Order.filled_avg_price.isnot(None),
    ]
    if mirrors_only:
        conds.append(Order.parent_order_id.isnot(None))
    orders: list[Order] = list(db.execute(select(Order).where(*conds)).scalars())

    # All Fill rows for those orders (one query, then bucket).
    order_ids = [o.id for o in orders]
    fills_by_order: dict[uuid.UUID, list[Fill]] = defaultdict(list)
    if order_ids:
        for f in db.execute(
            select(Fill).where(Fill.order_id.in_(order_ids))
        ).scalars():
            fills_by_order[f.order_id].append(f)

    # Flatten to a sortable timeline of (when, qty, price, order). If the order
    # has explicit fills, use them; otherwise synthesize one from the aggregate.
    timeline: list[tuple[datetime, Decimal, Decimal, Order]] = []
    for o in orders:
        fs = fills_by_order.get(o.id)
        if fs:
            for f in fs:
                timeline.append((f.filled_at, f.quantity, f.price, o))
        else:
            when = o.closed_at or o.submitted_at or o.created_at
            timeline.append((when, o.filled_quantity, o.filled_avg_price, o))
    timeline.sort(key=lambda e: e[0])

    bucket_tz = _tz_or_market(tz_name)
    open_lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
    daily: dict[date, tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))

    for filled_at, fill_qty, fill_price, order in timeline:
        key = _instrument_key(order)
        # Options P&L multiplier — 100 shares per contract for standard US options.
        unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
        qty = fill_qty
        price = fill_price
        day = filled_at.astimezone(bucket_tz).date()
        if start and day < start:
            pass  # we still need to walk earlier fills to keep lots correct
        if end and day > end:
            break

        if order.side == OrderSide.BUY:
            # Opening or closing a short — try to close shorts first (negative lots).
            if open_lots[key] and open_lots[key][0].qty < 0:
                pnl = Decimal(0)
                while qty > 0 and open_lots[key] and open_lots[key][0].qty < 0:
                    lot = open_lots[key][0]
                    take = min(qty, -lot.qty)
                    pnl += (lot.price - price) * take * unit
                    lot.qty += take
                    qty -= take
                    if lot.qty == 0:
                        open_lots[key].popleft()
                if start is None or day >= start:
                    cur_pnl, cur_n = daily[day]
                    daily[day] = (cur_pnl + pnl, cur_n + 1)
                if qty > 0:
                    open_lots[key].append(_Lot(qty=qty, price=price))
            else:
                open_lots[key].append(_Lot(qty=qty, price=price))
        else:  # SELL — close longs first
            if open_lots[key] and open_lots[key][0].qty > 0:
                pnl = Decimal(0)
                while qty > 0 and open_lots[key] and open_lots[key][0].qty > 0:
                    lot = open_lots[key][0]
                    take = min(qty, lot.qty)
                    pnl += (price - lot.price) * take * unit
                    lot.qty -= take
                    qty -= take
                    if lot.qty == 0:
                        open_lots[key].popleft()
                if start is None or day >= start:
                    cur_pnl, cur_n = daily[day]
                    daily[day] = (cur_pnl + pnl, cur_n + 1)
                if qty > 0:
                    open_lots[key].append(_Lot(qty=-qty, price=price))
            else:
                open_lots[key].append(_Lot(qty=-qty, price=price))

    return dict(daily)
