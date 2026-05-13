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
    closing fills on that day."""
    q = (
        select(Fill, Order)
        .join(Order, Fill.order_id == Order.id)
        .where(Order.user_id == user_id)
        .order_by(Fill.filled_at.asc())
    )
    rows: list[tuple[Fill, Order]] = list(db.execute(q).all())

    open_lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
    daily: dict[date, tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))

    for fill, order in rows:
        key = _instrument_key(order)
        # Options P&L multiplier — 100 shares per contract for standard US options.
        unit = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
        qty = fill.quantity
        price = fill.price
        day = fill.filled_at.astimezone(timezone.utc).date()
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
