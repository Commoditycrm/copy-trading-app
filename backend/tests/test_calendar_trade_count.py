"""Regression guard for the calendar's trade count.

`realized_pnl_by_day` must count DISTINCT CLOSING ORDERS per day, not closing
fills. A single closing order the broker executes in several partial fills is
ONE trade — counting fills made the calendar show more "N trades" than the user
placed (prod: trader placed 12, calendar showed 17).

Real in-memory SQLite against the actual ORM/FIFO. No broker, no network.

Run standalone:  .venv/bin/python tests/test_calendar_trade_count.py
Or under pytest: pytest tests/test_calendar_trade_count.py
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from sqlalchemy import create_engine

from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services.pnl import realized_pnl_by_day

_USER = uuid.uuid4()
_OPEN = datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)   # 09:30 ET
_C1 = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)      # 10:00 ET
_C2 = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
_C3 = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)
    Fill.__table__.create(eng)
    return Session(eng)


def _order(db, side, qty, price, *, when):
    o = Order(
        id=uuid.uuid4(),
        user_id=_USER,
        instrument_type=InstrumentType.STOCK,
        symbol="AAA",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(str(qty)),
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(str(qty)),
        filled_avg_price=Decimal(str(price)),
        created_at=when,
        closed_at=when,
    )
    db.add(o)
    db.flush()
    return o


def _fill(db, order, qty, price, when):
    db.add(Fill(id=uuid.uuid4(), order_id=order.id, quantity=Decimal(str(qty)),
                price=Decimal(str(price)), filled_at=when))
    db.flush()


def test_partial_fills_of_one_close_count_as_one_trade():
    """Open 3, then ONE closing order that fills in 3 partial fills → 1 trade."""
    db = _session()
    _order(db, OrderSide.BUY, 3, 100, when=_OPEN)  # synthesized single opening fill
    sell = _order(db, OrderSide.SELL, 3, 110, when=_C3)
    _fill(db, sell, 1, 110, _C1)
    _fill(db, sell, 1, 110, _C2)
    _fill(db, sell, 1, 110, _C3)

    daily = realized_pnl_by_day(db, _USER)
    assert len(daily) == 1
    (pnl, count) = next(iter(daily.values()))
    assert pnl == Decimal("30"), f"realized P&L should be (110-100)*3 = 30, got {pnl}"
    assert count == 1, f"one closing order = 1 trade, not per-fill; got {count}"


def test_separate_closing_orders_count_separately():
    """Open 2, then TWO distinct closing orders same day → 2 trades."""
    db = _session()
    _order(db, OrderSide.BUY, 2, 100, when=_OPEN)
    s1 = _order(db, OrderSide.SELL, 1, 110, when=_C1)
    _fill(db, s1, 1, 110, _C1)
    s2 = _order(db, OrderSide.SELL, 1, 120, when=_C2)
    _fill(db, s2, 1, 120, _C2)

    daily = realized_pnl_by_day(db, _USER)
    assert len(daily) == 1
    (pnl, count) = next(iter(daily.values()))
    assert pnl == Decimal("30"), f"(110-100)+(120-100) = 30, got {pnl}"
    assert count == 2, f"two distinct closing orders = 2 trades; got {count}"


if __name__ == "__main__":
    test_partial_fills_of_one_close_count_as_one_trade()
    test_separate_closing_orders_count_separately()
    print("ok")
