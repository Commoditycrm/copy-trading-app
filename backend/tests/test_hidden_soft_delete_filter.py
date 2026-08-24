"""Regression guard for the admin soft-delete ("Hide P&L") read filter.

Hiding is durable and centralized in services.visibility. This test seeds a
user with one VISIBLE closed round-trip and one HIDDEN one (plus a visible and
a hidden P&L snapshot), then asserts the shared read paths never surface the
hidden data. If a future change drops the filter — or someone adds a read path
that forgets it and routes through these helpers wrongly — this fails.

Real in-memory SQLite against the actual ORM queries. No broker, no network.

Run standalone:  .venv/bin/python tests/test_hidden_soft_delete_filter.py
Or under pytest: pytest tests/test_hidden_soft_delete_filter.py
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot as Snap
from app.services import trade_filters, visibility
from app.services.pnl import realized_pnl_by_day

_USER = uuid.uuid4()
_T1 = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)
    Fill.__table__.create(eng)
    Snap.__table__.create(eng)
    return Session(eng)


def _order(db, symbol, side, price, *, hidden):
    o = Order(
        id=uuid.uuid4(),
        user_id=_USER,
        instrument_type=InstrumentType.STOCK,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        status=OrderStatus.FILLED,
        filled_quantity=Decimal("1"),
        filled_avg_price=Decimal(str(price)),
        created_at=_T1,
        closed_at=(_T1 if side == OrderSide.BUY else _T2),
        hidden_at=(_T2 if hidden else None),
    )
    db.add(o)
    db.flush()
    return o


def _seed(db):
    # Visible round-trip: buy 100 -> sell 110 => +10 realized.
    _order(db, "AAA", OrderSide.BUY, 100, hidden=False)
    _order(db, "AAA", OrderSide.SELL, 110, hidden=False)
    # Hidden round-trip: buy 100 -> sell 200 => +100 realized, but hidden.
    _order(db, "BBB", OrderSide.BUY, 100, hidden=True)
    _order(db, "BBB", OrderSide.SELL, 200, hidden=True)
    # One visible + one hidden P&L snapshot day.
    db.add(Snap(id=uuid.uuid4(), user_id=_USER, day=date(2026, 8, 3),
                realized_pnl=Decimal("10"), trade_count=1, source="db_realized",
                hidden=False, computed_at=_T2))
    db.add(Snap(id=uuid.uuid4(), user_id=_USER, day=date(2026, 8, 2),
                realized_pnl=Decimal("100"), trade_count=1, source="db_realized",
                hidden=True, computed_at=_T2))
    db.flush()


def test_realized_pnl_excludes_hidden_orders():
    db = _session()
    _seed(db)
    daily = realized_pnl_by_day(db, _USER)
    total = sum(pnl for pnl, _ in daily.values())
    assert total == Decimal("10"), f"hidden +100 leaked into realized P&L: {total}"


def test_order_select_helper_excludes_hidden():
    db = _session()
    _seed(db)
    rows = db.execute(
        trade_filters.exclude_hidden(select(Order.symbol)).where(Order.user_id == _USER)
    ).scalars().all()
    assert set(rows) == {"AAA"}, f"hidden symbol leaked: {sorted(set(rows))}"


def test_snapshot_helper_excludes_hidden():
    db = _session()
    _seed(db)
    days = db.execute(
        visibility.visible_snapshots(select(Snap.day)).where(Snap.user_id == _USER)
    ).scalars().all()
    assert set(days) == {date(2026, 8, 3)}, f"hidden snapshot day leaked: {days}"


if __name__ == "__main__":
    test_realized_pnl_excludes_hidden_orders()
    test_order_select_helper_excludes_hidden()
    test_snapshot_helper_excludes_hidden()
    print("all soft-delete filter tests passed")
