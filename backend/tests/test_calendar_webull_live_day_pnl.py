"""Today's calendar cell shows the broker's OWN Day's P&L for a Webull account.

Webull reports total_day_profit_loss directly; our reconstructed realized +
unrealized swing does not match it. The calendar must DISPLAY the broker figure
for today (via marked_pnl) while leaving realized / unrealized as the calculated
values. A broker 0.00 must display 0.00, never fall back to the reconstruction.

Real in-memory SQLite + the actual endpoint function; broker calls monkeypatched.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import trades
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.models.order import Fill, Order
from app.models.user import User, UserRole
from app.services import market_hours


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    for t in (User, BrokerAccount, Order, Fill, DailyRealizedPnlSnapshot):
        t.__table__.create(eng)
    return Session(eng)


def _setup(monkeypatch, day_pnl):
    db = _session()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@t.co", password_hash="x",
                role=UserRole.TRADER, is_active=True)
    db.add(user)
    db.add(BrokerAccount(
        id=uuid.uuid4(), user_id=user.id, broker=BrokerName.WEBULL, label="wb",
        is_paper=True, supports_fractional=True, encrypted_credentials="x",
        connection_status="connected",
    ))
    db.flush()
    # Neutralise every broker-touching call except the one under test.
    monkeypatch.setattr(trades.fills_sync, "sync_user_fills",
                        lambda *a, **k: {"fills_added": 0, "orders_added": 0})
    monkeypatch.setattr(trades, "_live_unrealized_today", lambda *a, **k: Decimal("-681"))
    # _live_day_pnl_today returns (value, source) | None.
    monkeypatch.setattr(trades, "_live_day_pnl_today",
                        lambda *a, **k: None if day_pnl is None else (day_pnl, "webull_live"))
    return db, user


def _today_cell(monkeypatch, day_pnl):
    db, user = _setup(monkeypatch, day_pnl)
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=today, to=today, tz="America/New_York",
                               user_id=None)
    return next(r for r in rows if r.day == today)


def test_webull_today_displays_broker_day_pnl(monkeypatch):
    cell = _today_cell(monkeypatch, Decimal("-150.72"))
    assert cell.marked_pnl == Decimal("-150.72"), "displayed marked must be the broker's Day P&L"
    assert cell.source == "webull_live"
    assert cell.realized_pnl == Decimal(0), "realized stays the calculated figure"
    # unrealized (the calculated swing) is untouched — NOT overwritten to -150.72.
    assert cell.unrealized_pnl != Decimal("-150.72")


def test_webull_today_zero_day_pnl_shows_zero(monkeypatch):
    cell = _today_cell(monkeypatch, Decimal("0"))
    assert cell.marked_pnl == Decimal("0"), "a broker 0.00 must display 0.00, not fall back"
    assert cell.source == "webull_live"


def test_alpaca_today_uses_broker_live_source(monkeypatch):
    """Today follows whichever broker is connected — an Alpaca account's live
    equity−last_equity comes through with an alpaca_live source label."""
    db, user = _setup(monkeypatch, Decimal("321.00"))
    monkeypatch.setattr(trades, "_live_day_pnl_today",
                        lambda *a, **k: (Decimal("321.00"), "alpaca_live"))
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=today, to=today,
                               tz="America/New_York", user_id=None)
    cell = next(r for r in rows if r.day == today)
    assert cell.marked_pnl == Decimal("321.00")
    assert cell.source == "alpaca_live"
    assert cell.quality == "authoritative"


def test_no_live_day_pnl_leaves_marked_none(monkeypatch):
    # e.g. Alpaca / mixed account → _live_day_pnl_today returns None.
    cell = _today_cell(monkeypatch, None)
    assert cell.marked_pnl is None, "no broker figure → frontend falls back to realized+unrealized"
    assert cell.source == "calculated"


def test_historical_day_stays_finalized_during_live_refresh(monkeypatch):
    """A past day with a finalized (eod) snapshot is NOT marked live and does NOT
    pick up today's live value — the live path only touches today's cell."""
    db, user = _setup(monkeypatch, Decimal("-150.72"))
    past = date(2026, 9, 15)  # a past Monday, well before "today"
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=user.id, day=past, realized_pnl=Decimal("42"),
        trade_count=0, source="marked", snapshot_type="eod",
        eod_unrealized=Decimal("-10"), hidden=False,
    ))
    db.flush()
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                               tz="America/New_York", user_id=None)
    todc = next(r for r in rows if r.day == today)
    pastc = next(r for r in rows if r.day == past)
    # Today: live broker value.
    assert todc.live is True and todc.marked_pnl == Decimal("-150.72")
    assert todc.source == "webull_live"
    # Past: finalized, NOT live, and never tagged with the live number/source.
    assert pastc.live is False
    assert pastc.marked_pnl is None            # live value doesn't leak to history
    assert pastc.source != "webull_live"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
