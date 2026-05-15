"""Realized P&L calculation from fills.

Per-user, per-symbol, per-instrument FIFO matching. Open lots roll forward.
For options we key on the full contract identity (symbol + expiry + strike + right).
For now we ignore commissions/fees beyond the per-fill `fee` column.

Returns daily realized P&L within [start, end] inclusive (UTC dates).
"""
from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Fill, InstrumentType, Order, OrderSide


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


def today_realized_pnl(db: Session, user_id: uuid.UUID) -> Decimal:
    """Realized P&L for the current UTC day. Negative = loss.

    Uses the same FIFO matching as `realized_pnl_by_day`. Cheap enough at
    small fill counts; cache or materialize once you have many fills/user/day.
    """
    today = datetime.now(timezone.utc).date()
    daily = realized_pnl_by_day(db, user_id, start=today, end=today)
    pnl, _ = daily.get(today, (Decimal(0), 0))
    return pnl


def realized_pnl_by_day(
    db: Session, user_id: uuid.UUID, start: date | None = None, end: date | None = None
) -> dict[date, tuple[Decimal, int]]:
    """Returns {day: (realized_pnl, trade_count)}. trade_count is the number of
    closing fills on that day.

    Source of truth is the `fills` table. For freshly filled orders whose
    detailed Fill rows haven't synced from the broker's activity feed yet,
    we synthesize a single fill from the order's aggregate `filled_quantity`
    + `filled_avg_price` so P&L shows up immediately instead of lagging
    minutes behind the broker.
    """
    # All orders the user owns that have any fill quantity recorded.
    orders: list[Order] = list(db.execute(
        select(Order).where(
            Order.user_id == user_id,
            Order.filled_quantity > 0,
            Order.filled_avg_price.isnot(None),
        )
    ).scalars())

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

    open_lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
    daily: dict[date, tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))

    for filled_at, fill_qty, fill_price, order in timeline:
        key = _instrument_key(order)
        # Options P&L multiplier — 100 shares per contract for standard US options.
        unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
        qty = fill_qty
        price = fill_price
        day = filled_at.astimezone(timezone.utc).date()
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
