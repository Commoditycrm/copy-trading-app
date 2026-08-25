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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.models.order import Fill, InstrumentType, Order, OrderSide
from app.services import visibility

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


def reconstruct_marked_series(
    days: list[date],
    realized_by_day: dict[date, Decimal],
    eod_unreal_by_day: dict[date, Decimal],
) -> dict[date, Decimal]:
    """Reconstruct MARKED daily P&L (realized + unrealized change) for brokers
    that expose no marked-history series (SnapTrade/Webull), from our own
    end-of-day unrealized captures.

    For each day D in ``days``::

        marked(D) = realized(D) + (eod(D) − eod(prev))

    where ``prev`` is the most recent EARLIER day that has a captured EOD
    unrealized value (markets skip weekends/holidays, so we diff against the
    last capture, not the literal calendar day). A day missing its own EOD
    capture, or with no earlier capture to diff against, falls back to
    realized-only — that's the forward-only property: days before we began
    capturing EOD unrealized stay realized-only, and true marked kicks in once
    two consecutive captures exist.

    ``eod_unreal_by_day`` should include some lookback beyond ``days`` so the
    first requested day can diff against a prior capture. The construction
    telescopes: summed over a position's whole life the Δunrealized terms
    cancel, so the marked total equals the realized total — no double-count.
    """
    eod_days = sorted(eod_unreal_by_day)
    out: dict[date, Decimal] = {}
    for d in days:
        r = Decimal(realized_by_day.get(d, Decimal(0)))
        ed = eod_unreal_by_day.get(d)
        if ed is None:
            out[d] = r
            continue
        prev = None
        for pd in reversed(eod_days):
            if pd < d:
                prev = pd
                break
        out[d] = r if prev is None else r + (Decimal(ed) - Decimal(eod_unreal_by_day[prev]))
    return out


def _tz_or_market(tz_name: str | None) -> "ZoneInfo | timezone":
    """Resolve the bucketing timezone. Falls back to the market timezone if
    the caller didn't supply one or the name is unknown."""
    if not tz_name:
        return _MARKET_TZ
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return _MARKET_TZ


def today_buy_notional(
    db: Session, user_id: uuid.UUID, tz_name: str | None = None,
) -> Decimal:
    """Cumulative USD value of every BUY fill placed today for ``user_id``.

    Returns ``sum(abs(filled_qty) * filled_avg_price * multiplier)`` across
    every BUY order filled in today's market-timezone day. ONLY buys count —
    this is the cash spent OPENING/adding today. A SELL does NOT reduce it: the
    daily budget is spend-based, not net turnover, so the running total only
    ever goes UP within the day (selling never gives budget back). Options pick
    up the 100x contract multiplier.

    Used by the per-day ``max_account_usd/pct_per_day`` cap in
    ``services.pnl_poller``: once today's cumulative buy value crosses the
    budget, copy is auto-paused for the day (auto-resumes next day).
    """
    tz = _tz_or_market(tz_name)
    today = datetime.now(tz).date()

    orders = list(db.execute(
        select(Order).where(
            Order.user_id == user_id,
            Order.side == OrderSide.BUY,
            Order.filled_quantity > 0,
            Order.filled_avg_price.isnot(None),
            visibility.order_is_visible(),
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
            visibility.order_is_visible(),
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


def dedupe_subscriber_orders(orders_all: list[Order]) -> list[Order]:
    """Drop the SnapTrade listener's duplicate STANDALONE rows, keeping every
    real fill exactly once.

    The listener re-records a mirror's broker fill as a standalone row carrying
    the SAME broker_order_id — those must not double-count. But a subscriber
    also has genuine broker-side fills (closes placed directly at their broker,
    and reconnect-orphaned rows whose broker_account went NULL) that arrive ONLY
    as standalone rows, each with its own broker_order_id. Rule: drop a
    standalone only when its broker_order_id already appears on a mirror (a true
    duplicate); keep standalone rows with a unique/absent broker_order_id.

    This is the single source of truth for "which orders are the subscriber's
    real trades" — realized_pnl_by_day and the position reconciler both call it
    so their views can never disagree (that disagreement was the false-phantom
    bug: the reconciler counted raw fills and saw positions the P&L FIFO had
    already closed).
    """
    mirror_boids = {
        o.broker_order_id
        for o in orders_all
        if o.parent_order_id is not None and o.broker_order_id
    }
    return [
        o for o in orders_all
        if o.parent_order_id is not None            # a mirror — always keep
        or not o.broker_order_id                    # no id to dedupe on — keep
        or o.broker_order_id not in mirror_boids     # unique broker-side fill
    ]


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
    # Orders the user owns with any fill recorded. hidden_at excludes
    # admin-soft-deleted orders from the FIFO entirely (they don't exist for
    # P&L purposes) — see api/admin.hide_user_orders.
    conds = [
        Order.user_id == user_id,
        Order.filled_quantity > 0,
        Order.filled_avg_price.isnot(None),
        visibility.order_is_visible(),
    ]
    orders_all: list[Order] = list(db.execute(select(Order).where(*conds)).scalars())

    if mirrors_only:
        # Subscriber de-duplication, by broker_order_id — NOT "mirrors only".
        # See dedupe_subscriber_orders for the full rationale. The old rule
        # "drop every standalone" killed the subscriber's real broker-side
        # closes, leaving positions open in the FIFO and skewing realized P&L.
        orders: list[Order] = dedupe_subscriber_orders(orders_all)
    else:
        orders = orders_all

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


def realized_pnl_by_order(
    db: Session, user_id: uuid.UUID, mirrors_only: bool = False,
) -> dict[uuid.UUID, Decimal]:
    """Realized P&L attributed to each CLOSING order — the order whose fill
    reduced/closed a position — using the SAME FIFO as realized_pnl_by_day.

    Opening orders never appear (they realize nothing until closed). Lets the UI
    show a per-trade P&L. Walks the user's whole history so cost basis is right,
    then returns only orders that produced a non-zero realized amount.
    """
    conds = [
        Order.user_id == user_id,
        Order.filled_quantity > 0,
        Order.filled_avg_price.isnot(None),
        visibility.order_is_visible(),
    ]
    orders_all: list[Order] = list(db.execute(select(Order).where(*conds)).scalars())
    orders = dedupe_subscriber_orders(orders_all) if mirrors_only else orders_all

    order_ids = [o.id for o in orders]
    fills_by_order: dict[uuid.UUID, list[Fill]] = defaultdict(list)
    if order_ids:
        for f in db.execute(select(Fill).where(Fill.order_id.in_(order_ids))).scalars():
            fills_by_order[f.order_id].append(f)

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

    open_lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
    by_order: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for _when, fill_qty, fill_price, order in timeline:
        key = _instrument_key(order)
        unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
        qty, price = fill_qty, fill_price
        if order.side == OrderSide.BUY:
            if open_lots[key] and open_lots[key][0].qty < 0:  # cover shorts
                while qty > 0 and open_lots[key] and open_lots[key][0].qty < 0:
                    lot = open_lots[key][0]
                    take = min(qty, -lot.qty)
                    by_order[order.id] += (lot.price - price) * take * unit
                    lot.qty += take
                    qty -= take
                    if lot.qty == 0:
                        open_lots[key].popleft()
                if qty > 0:
                    open_lots[key].append(_Lot(qty=qty, price=price))
            else:
                open_lots[key].append(_Lot(qty=qty, price=price))
        else:  # SELL — close longs
            if open_lots[key] and open_lots[key][0].qty > 0:
                while qty > 0 and open_lots[key] and open_lots[key][0].qty > 0:
                    lot = open_lots[key][0]
                    take = min(qty, lot.qty)
                    by_order[order.id] += (price - lot.price) * take * unit
                    lot.qty -= take
                    qty -= take
                    if lot.qty == 0:
                        open_lots[key].popleft()
                if qty > 0:
                    open_lots[key].append(_Lot(qty=-qty, price=price))
            else:
                open_lots[key].append(_Lot(qty=-qty, price=price))

    return {oid: p for oid, p in by_order.items() if p != 0}


# ── Broker-agnostic calendar P&L (realized from order history + unrealized from
#    our own position captures) ──────────────────────────────────────────────
# One model for EVERY broker. Realized comes straight from our order history
# (realized_pnl_by_day — FIFO over fills), never from a broker feed/snapshot.
# Unrealized comes from the end-of-day position captures we already record
# (DailyRealizedPnlSnapshot.eod_unrealized). The two combine as the same
# telescoping marked series the calendar used before:
#     marked(D) = realized(D) + (eod(D) − eod(prev capture))
# Summed over a position's life the Δunrealized terms cancel, so the marked
# total equals the realized total — no double-count. This replaces the old
# per-broker branching, the broker-activity realized snapshot, and the Alpaca
# portfolio-history path.

# Lookback beyond the visible range so the first shown day can diff its EOD
# unrealized against an earlier capture (markets skip weekends/holidays, so we
# diff against the last capture, not the literal prior day).
_EOD_LOOKBACK_DAYS = 21


@dataclass
class CalendarDay:
    """One calendar cell. ``marked_pnl`` is the number shown (realized +
    Δunrealized). ``realized_pnl`` is the realized-only component. For TODAY,
    ``unrealized_pnl`` surfaces the open-position swing (marked − realized) and
    ``live`` is True; both are None/False on settled days."""

    day: date
    marked_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    trade_count: int
    live: bool


def load_eod_unrealized(
    db: Session, user_id: uuid.UUID, start: date, end: date,
) -> dict[date, Decimal]:
    """Per-day end-of-day unrealized captures for [start, end] — the unrealized
    half of the marked reconstruction. Honors the soft-delete visibility filter.
    Pass a start well before the visible range so the first shown day can diff
    against a prior capture."""
    rows = db.execute(
        select(
            DailyRealizedPnlSnapshot.day,
            DailyRealizedPnlSnapshot.eod_unrealized,
        ).where(
            DailyRealizedPnlSnapshot.user_id == user_id,
            DailyRealizedPnlSnapshot.day >= start,
            DailyRealizedPnlSnapshot.day <= end,
            DailyRealizedPnlSnapshot.eod_unrealized.isnot(None),
            visibility.snapshot_is_visible(),
        )
    ).all()
    return {d: Decimal(v) for d, v in rows if v is not None}


def today_live_cell(
    realized_today: Decimal,
    live_unrealized: Decimal,
    prior_close_eod: Decimal,
) -> tuple[Decimal, Decimal]:
    """TODAY's cell under the overnight-reset rule.

    Today's unrealized is measured from the PRIOR CLOSE — yesterday's captured
    end-of-day unrealized (``prior_close_eod``) — NOT from the position's entry.
    So a position carried overnight starts today's swing at zero; only the move
    that happened TODAY counts. A position OPENED today diffs against the prior
    (flat) capture ≈ 0, so it shows its full entry→now move.

        day_unrealized = live_unrealized − prior_close_eod
        marked         = realized_today + day_unrealized

    Returns ``(marked, day_unrealized)``."""
    day_unrealized = Decimal(live_unrealized) - Decimal(prior_close_eod)
    return Decimal(realized_today) + day_unrealized, day_unrealized


def calendar_series(
    db: Session,
    user_id: uuid.UUID,
    from_: date,
    to: date,
    tz_name: str | None = None,
    mirrors_only: bool = False,
    live_today_unrealized: Decimal | None = None,
) -> dict[date, CalendarDay]:
    """Daily P&L for the calendar, broker-agnostic.

    Each cell answers "how much did I make/lose THAT day": realized (FIFO over
    order history) + that day's UNREALIZED SWING — the change in open-position
    mark since the prior close, NOT the cumulative move from entry. So a position
    carried overnight locks yesterday's swing into yesterday and starts today's
    from zero.

    * Past days use our end-of-day unrealized captures via
      ``reconstruct_marked_series``: ``marked(D) = realized(D) + (eod(D) − eod(prev))``.
    * TODAY, when the caller passes ``live_today_unrealized`` (the current summed
      open-position unrealized, fetched at page-load), shows
      ``realized(today) + (live − prior close)`` so the in-progress day reflects
      the CURRENT price, reset from yesterday. If it's not supplied (broker
      unavailable), today falls back to its latest EOD capture.

    Pure DB except for the single live figure the caller passes in. Weekends
    never produce a cell."""
    realized = realized_pnl_by_day(
        db, user_id, start=from_, end=to, tz_name=tz_name, mirrors_only=mirrors_only
    )
    realized_by_day = {d: Decimal(p) for d, (p, _c) in realized.items()}
    counts = {d: c for d, (_p, c) in realized.items()}

    eod = load_eod_unrealized(
        db, user_id, from_ - timedelta(days=_EOD_LOOKBACK_DAYS), to
    )

    tz = _tz_or_market(tz_name)
    today = datetime.now(tz).date()

    # Prior-close baseline for today: most recent captured EOD strictly before
    # today (markets skip weekends, so it's the last *session's* close).
    prior_close_eod: Decimal | None = None
    for pd in sorted((d for d in eod if d < today), reverse=True):
        prior_close_eod = eod[pd]
        break

    # Weekends never hold a US session — drop them so a stale carried EOD capture
    # (or a mis-bucketed fill) can't paint a weekend cell.
    days = {
        d for d in (set(realized_by_day) | set(eod))
        if from_ <= d <= to and d.weekday() < 5
    }
    # TODAY is live when the caller supplied the current unrealized AND we have a
    # prior close to reset the day's swing from.
    today_live = (
        live_today_unrealized is not None
        and from_ <= today <= to
        and today.weekday() < 5
        and prior_close_eod is not None
    )
    if today_live:
        days.add(today)

    ordered = sorted(days)
    marked = reconstruct_marked_series(ordered, realized_by_day, eod)

    out: dict[date, CalendarDay] = {}
    for d in ordered:
        r = realized_by_day.get(d, Decimal(0))
        if d == today and today_live:
            m, day_unreal = today_live_cell(r, live_today_unrealized, prior_close_eod)
            out[d] = CalendarDay(d, m, r, day_unreal, counts.get(d, 0), True)
        elif d == today and d in eod and prior_close_eod is not None:
            # No live value (broker down), but today has its own EOD capture and
            # a prior close → fall back to the captured swing, still flagged live.
            m = marked[d]
            out[d] = CalendarDay(d, m, r, m - r, counts.get(d, 0), True)
        else:
            out[d] = CalendarDay(d, marked.get(d, r), r, None, counts.get(d, 0), False)
    return out
