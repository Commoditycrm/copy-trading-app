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

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.models.order import Fill, InstrumentType, Order, OrderSide
from app.services import visibility

log = logging.getLogger(__name__)

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
    # Broker that placed the opening fill — only used to time an expiry booking
    # (Alpaca reflects it on the expiry day; Webull the next business day). None
    # for a disconnected broker; treated as the next-business-day default.
    broker: "BrokerName | None" = None


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


def _next_business_day(d: date) -> date:
    """The next Mon–Fri after ``d`` — where the broker reflects an option expiry
    in the day's P&L. Webull books an expired-worthless long the NEXT business
    day (not the expiry day itself). Holiday-agnostic: a market holiday would put
    it one weekday early, an acceptable edge for a daily P&L calendar."""
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:                # Sat/Sun
        nd += timedelta(days=1)
    return nd


def _dedup_fills(rows):
    """Yield fills, skipping a repeated broker_fill_id. A duplicate fill row
    (same broker fill recorded twice by racing sync paths) would double-count a
    lot and flip FIFO realized P&L. NULL ids are distinct (synthetic fills), so
    they pass through. Belt-and-braces alongside the unique index on the column."""
    seen: set[str] = set()
    for f in rows:
        bid = f.broker_fill_id
        if bid is not None:
            if bid in seen:
                continue
            seen.add(bid)
        yield f


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
    for f in _dedup_fills(db.execute(
        select(Fill).where(Fill.order_id.in_(order_ids))
    ).scalars()):
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
    for f in _dedup_fills(db.execute(
        select(Fill).where(Fill.order_id.in_(order_ids))
    ).scalars()):
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
    distinct closing ORDERS on that day — a single order that the broker fills in
    several partial fills counts once, so the calendar's "N trades" matches what
    the user placed rather than the raw fill count.

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

    # Broker per account, to time an expiry booking (Alpaca on the expiry day,
    # everyone else the next business day). Only needed when the user actually
    # holds options — a stock-only history never books an expiry — and tolerant
    # of a minimal test schema that omits broker_accounts (falls back to the
    # next-business-day default).
    acct_broker: dict[uuid.UUID, BrokerName] = {}
    if any(o.instrument_type == InstrumentType.OPTION for o in orders):
        try:
            acct_broker = {
                aid: br
                for aid, br in db.execute(
                    select(BrokerAccount.id, BrokerAccount.broker).where(
                        BrokerAccount.user_id == user_id
                    )
                ).all()
            }
        except SQLAlchemyError:
            acct_broker = {}

    # All Fill rows for those orders (one query, then bucket).
    order_ids = [o.id for o in orders]
    fills_by_order: dict[uuid.UUID, list[Fill]] = defaultdict(list)
    if order_ids:
        for f in _dedup_fills(db.execute(
            select(Fill).where(Fill.order_id.in_(order_ids))
        ).scalars()):
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
    # Realized P&L per day, and the set of CLOSING ORDER ids per day. trade_count
    # is len(that set): a closing order that the broker fills in several partial
    # fills is ONE trade, not one per fill — counting fills made the calendar
    # show more "trades" than the user placed.
    daily_pnl: dict[date, Decimal] = defaultdict(Decimal)
    closing_orders: dict[date, set[uuid.UUID]] = defaultdict(set)

    for filled_at, fill_qty, fill_price, order in timeline:
        key = _instrument_key(order)
        # Options P&L multiplier — 100 shares per contract for standard US options.
        unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
        broker = acct_broker.get(order.broker_account_id) if order.broker_account_id else None
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
                    daily_pnl[day] += pnl
                    closing_orders[day].add(order.id)
                if qty > 0:
                    open_lots[key].append(_Lot(qty=qty, price=price, broker=broker))
            else:
                open_lots[key].append(_Lot(qty=qty, price=price, broker=broker))
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
                    daily_pnl[day] += pnl
                    closing_orders[day].add(order.id)
                if qty > 0:
                    open_lots[key].append(_Lot(qty=-qty, price=price, broker=broker))
            else:
                open_lots[key].append(_Lot(qty=-qty, price=price, broker=broker))

    # ── Book expired options that were let-lapse (worthless) ─────────────────
    # An option still open past its expiry was never closed by a fill, so a
    # fill-based FIFO leaves the lot open forever and drops its P&L. The broker
    # settles it at expiry, and for a lapse (finished out-of-the-money) that
    # settlement is $0:
    #   * a LONG lot loses the full premium it paid  (Webull "+$1,339 → +$475"),
    #   * a SHORT lot keeps the full premium it collected (Alpaca OPEXP net=0 on
    #     arsalan's SPXW puts — the gain our FIFO was missing).
    # Closing every remaining lot at $0 gives both signs for free:
    #   (0 − price) × qty × 100  →  negative for qty>0 (long), positive for qty<0.
    #
    # CAPABILITY GATE — the load-bearing safety. This inference ("still open past
    # expiry ⇒ expired worthless") is only valid when we have the broker's
    # COMPLETE fill history. Without it (Webull direct), a lot looks open only
    # because we never received its closing fill, and booking $0 invents a
    # phantom loss — gaurav's realized read −$19,904 vs Webull's −$6,627, almost
    # entirely from this. So we book expiries ONLY for brokers whose capability
    # says their fill history is authoritative, keyed off the lot's broker.
    #
    # ITM / cash-settled expiries settle at a NON-zero value only the broker's
    # settlement feed knows (Alpaca OPEXP.net_amount) — a documented follow-up.
    # Timing matches the broker app: Alpaca reflects the expiry ON the expiry
    # day (OPEXP.date == expiry), everyone else on the NEXT business day.
    from app.brokers.capabilities import capabilities_for  # local — avoid cycle
    today = datetime.now(bucket_tz).date()
    for key, lots in open_lots.items():
        if not lots or key[0] != "OPT":
            continue
        exp = key[2]                        # option_expiry date from _instrument_key
        if exp is None:
            continue
        # Lots on one contract share a broker, so the first lot's broker decides.
        broker = lots[0].broker
        if not capabilities_for(broker).authoritative_fill_history:
            continue                        # can't prove worthless expiry without
                                            # a complete fill feed (Webull direct)
        book_day = exp if broker == BrokerName.ALPACA else _next_business_day(exp)
        if book_day >= today:
            continue                        # only settled past days — never today,
                                            # whose expired-but-not-yet-removed lot
                                            # is still in the live-unrealized cell
                                            # (double-count guard)
        if (start and book_day < start) or (end and book_day > end):
            continue                        # booking day outside the queried window
        pnl = Decimal(0)
        for lot in lots:                    # long AND short — settle at $0
            pnl += (Decimal(0) - lot.price) * lot.qty * Decimal(100)
        if pnl != 0:
            daily_pnl[book_day] += pnl
            # One synthetic "trade" for the contract's expiry, deterministic so
            # re-runs don't inflate the count.
            closing_orders[book_day].add(uuid.uuid5(uuid.NAMESPACE_OID, f"exp:{key}"))

    return {
        d: (daily_pnl.get(d, Decimal(0)), len(closing_orders[d]))
        for d in closing_orders
    }


def realized_pnl_by_order(
    db: Session, user_id: uuid.UUID, mirrors_only: bool = False,
) -> dict[uuid.UUID, Decimal]:
    """Realized P&L attributed to each CLOSING order — the order whose fill
    reduced/closed a position — by the same FIFO walk as realized_pnl_by_day.

    Opening orders never appear (they realize nothing until closed). Lets the UI
    show a per-trade P&L. Walks the user's whole history so cost basis is right,
    then returns only orders that produced a non-zero realized amount.

    One difference from the by-day walk: an order flagged ``is_closing`` never
    OPENS a lot here (see the loop). Per-order attribution is what the trader
    reads row by row, and one close with no known entry must not shift every
    later P&L on that contract from the exits onto the entries.
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
        for f in _dedup_fills(db.execute(select(Fill).where(Fill.order_id.in_(order_ids))).scalars()):
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
        lots = open_lots[key]
        buying = order.side == OrderSide.BUY
        # A buy covers shorts, a sell closes longs — oldest lot first.
        while qty > 0 and lots and (lots[0].qty < 0 if buying else lots[0].qty > 0):
            lot = lots[0]
            take = min(qty, abs(lot.qty))
            by_order[order.id] += ((lot.price - price) if buying else (price - lot.price)) * take * unit
            lot.qty += take if buying else -take
            qty -= take
            if lot.qty == 0:
                lots.popleft()
        # What is left over opens a position — unless the order is a CLOSE. A
        # close whose entry isn't in this history (hidden, or opened outside the
        # app) realizes nothing we can price; booking it as a new short would
        # make the NEXT buy look like the close, and every later exit on the
        # contract would show no P&L while its entries did.
        if qty > 0 and not order.is_closing:
            lots.append(_Lot(qty=qty if buying else -qty, price=price))

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


def alpaca_marked_by_day(
    db: Session,
    user_id: uuid.UUID,
    from_: date,
    to: date,
    tz_name: str | None = None,
) -> dict[date, tuple[Decimal, Decimal | None]]:
    """Alpaca's OWN per-day MARKED P&L (+ daily return %) straight from its
    portfolio-history endpoint — the exact figure Alpaca's app shows on its
    calendar — summed across the user's connected Alpaca accounts.

    Returns {day: (marked, pct)}. Empty when the user has no connected Alpaca
    account, or the broker call fails, so the caller keeps its own reconstructed
    marked. Excludes TODAY: Alpaca's 1D portfolio-history omits the current
    intraday day, so the caller keeps the live cell for today. ``pct`` is only
    meaningful with a single Alpaca account; None when several are summed."""
    from app.brokers import adapter_for  # local import — avoid an import cycle
    from app.services.crypto import decrypt_json

    try:
        accts = list(db.execute(
            select(BrokerAccount).where(
                BrokerAccount.user_id == user_id,
                BrokerAccount.broker == BrokerName.ALPACA,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())
    except SQLAlchemyError:
        return {}

    out: dict[date, tuple[Decimal, Decimal | None]] = {}
    single = len(accts) == 1
    for acct in accts:
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            daily = adapter.marked_pnl_by_day(from_, to, tz_name)
        except Exception:  # noqa: BLE001 — a bad account must not blank the calendar
            log.warning("alpaca_marked_by_day: failed for acct %s", acct.id, exc_info=True)
            continue
        for d, vals in daily.items():
            marked = Decimal(vals[0])
            pct = vals[2] if len(vals) > 2 else None
            if d in out:
                prev_m, _ = out[d]
                out[d] = (prev_m + marked, None)  # summed accounts: % is undefined
            else:
                out[d] = (marked, pct if single else None)
    return out


def frozen_marked_by_day(
    db: Session, user_id: uuid.UUID, start: date, end: date,
) -> dict[date, tuple[Decimal, Decimal | None]]:
    """Broker-direct MARKED P&L per day the snapshot job FINALIZED at the close
    (``source='marked'`` AND ``snapshot_type='eod'`` — Alpaca and Webull Day's
    P&L). Returns {day: (marked, pct)}. This is the ONLY marked source for a
    Webull account (no history endpoint), so a Webull calendar matches the broker
    only from the first finalized snapshot forward; intraday captures and legacy
    rows are excluded so a mid-day figure never masquerades as settled P&L.
    Honors the soft-delete visibility filter."""
    rows = db.execute(
        select(
            DailyRealizedPnlSnapshot.day,
            DailyRealizedPnlSnapshot.realized_pnl,
            DailyRealizedPnlSnapshot.pct,
        ).where(
            DailyRealizedPnlSnapshot.user_id == user_id,
            DailyRealizedPnlSnapshot.day >= start,
            DailyRealizedPnlSnapshot.day <= end,
            DailyRealizedPnlSnapshot.source == "marked",
            DailyRealizedPnlSnapshot.snapshot_type == "eod",
            visibility.snapshot_is_visible(),
        )
    ).all()
    return {d: (Decimal(m), pct) for d, m, pct in rows}


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
    # TODAY is live whenever the caller supplied the current unrealized. If we
    # have a prior close we reset the day's swing against it; if we DON'T (no
    # capture yet — deploy day, or none recorded for this account), we reset
    # against ZERO so today still shows the full current open-position P&L
    # instead of dropping it to $0 (prod 2026-08-25: an account −$9,879 open but
    # showing $0 because it had no prior capture). No double-count: with no prior
    # capture, past days showed no unrealized, so this figure was never
    # attributed anywhere; once today's EOD is captured, tomorrow resets off it.
    today_live = (
        live_today_unrealized is not None
        and from_ <= today <= to
        and today.weekday() < 5
    )
    if today_live:
        days.add(today)

    ordered = sorted(days)
    marked = reconstruct_marked_series(ordered, realized_by_day, eod)

    out: dict[date, CalendarDay] = {}
    for d in ordered:
        r = realized_by_day.get(d, Decimal(0))
        if d == today and today_live:
            baseline = prior_close_eod if prior_close_eod is not None else Decimal(0)
            m, day_unreal = today_live_cell(r, live_today_unrealized, baseline)
            out[d] = CalendarDay(d, m, r, day_unreal, counts.get(d, 0), True)
        elif d == today and d in eod and prior_close_eod is not None:
            # No live value (broker down), but today has its own EOD capture and
            # a prior close → fall back to the captured swing, still flagged live.
            m = marked[d]
            out[d] = CalendarDay(d, m, r, m - r, counts.get(d, 0), True)
        else:
            out[d] = CalendarDay(d, marked.get(d, r), r, None, counts.get(d, 0), False)
    return out
