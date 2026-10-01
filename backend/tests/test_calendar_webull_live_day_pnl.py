"""Today's calendar cell shows the broker's OWN Day's P&L for a Webull account.

Webull reports total_day_profit_loss directly; our reconstructed realized +
unrealized swing does not match it. The calendar must DISPLAY the broker figure
for today (via marked_pnl) while leaving realized / unrealized as the calculated
values. A broker 0.00 must display 0.00, never fall back to the reconstruction.

Real in-memory SQLite + the actual endpoint function; broker calls monkeypatched.
"""
import os
import sys
import types
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import trades
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.models.order import (
    Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType,
)
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
    # _live_day_pnl_today returns (value, pct, source) | None.
    monkeypatch.setattr(
        trades, "_live_day_pnl_today",
        lambda *a, **k: None if day_pnl is None else (day_pnl, Decimal("8.88"), "webull_live"),
    )
    return db, user


def _today_cell(monkeypatch, day_pnl):
    db, user = _setup(monkeypatch, day_pnl)
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=today, to=today, tz="America/New_York",
                               user_id=None)
    return next(r for r in rows if r.day == today)


def test_webull_today_displays_broker_day_pnl(monkeypatch):
    cell = _today_cell(monkeypatch, Decimal("-150.72"))
    assert cell.day_pnl == Decimal("-150.72"), "Day's P&L must be the broker's value"
    assert cell.day_pnl_pct == Decimal("8.88")
    assert cell.source == "webull_live"
    assert cell.quality == "authoritative"
    # Diagnostics untouched — realized stays calculated, not the broker figure.
    assert cell.realized_pnl == Decimal(0)
    assert cell.unrealized_pnl != Decimal("-150.72")


def test_webull_today_zero_day_pnl_shows_zero(monkeypatch):
    cell = _today_cell(monkeypatch, Decimal("0"))
    assert cell.day_pnl == Decimal("0"), "a broker 0.00 must show 0.00, not '--'"
    assert cell.source == "webull_live"


def test_alpaca_today_uses_broker_live_source(monkeypatch):
    """Today follows whichever broker is connected — an Alpaca account's live
    equity−last_equity comes through with an alpaca_live source label."""
    db, user = _setup(monkeypatch, Decimal("321.00"))
    monkeypatch.setattr(trades, "_live_day_pnl_today",
                        lambda *a, **k: (Decimal("321.00"), Decimal("1.23"), "alpaca_live"))
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=today, to=today,
                               tz="America/New_York", user_id=None)
    cell = next(r for r in rows if r.day == today)
    assert cell.day_pnl == Decimal("321.00")
    assert cell.day_pnl_pct == Decimal("1.23")
    assert cell.source == "alpaca_live"
    assert cell.quality == "authoritative"


def test_no_live_day_pnl_is_unavailable(monkeypatch):
    # A connected broker with no live day P&L (e.g. IBKR) → unavailable, NOT the
    # reconstruction.
    cell = _today_cell(monkeypatch, None)
    assert cell.day_pnl is None
    assert cell.source == "none"
    assert cell.quality == "unavailable"


def test_stale_fallback_when_live_fetch_fails(monkeypatch):
    """A failed live broker fetch → show the last-known broker value flagged
    STALE with its capture time, never a fake zero or the reconstruction."""
    db, user = _setup(monkeypatch, Decimal("-150.72"))
    # (None, None, source) = broker present but the live fetch failed.
    monkeypatch.setattr(trades, "_live_day_pnl_today",
                        lambda *a, **k: (None, None, "webull_live"))
    today = market_hours.now_et().date()
    cap = datetime(2026, 9, 30, 18, 5, tzinfo=timezone.utc)
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=user.id, day=today, realized_pnl=Decimal("-140.00"),
        pct=Decimal("-1.62"), trade_count=0, source="marked", snapshot_type="intraday",
        hidden=False, computed_at=cap,
    ))
    db.flush()
    rows = trades.calendar_pnl(db=db, user=user, from_=today, to=today,
                               tz="America/New_York", user_id=None)
    cell = next(r for r in rows if r.day == today)
    assert cell.day_pnl == Decimal("-140.00"), "must show last-known broker value, not 0/calc"
    assert cell.day_pnl_pct == Decimal("-1.62")
    assert cell.source == "webull_live"
    assert cell.quality == "stale"
    # sqlite drops tzinfo; compare the wall-clock components.
    assert cell.last_updated_at is not None
    assert cell.last_updated_at.replace(tzinfo=None) == cap.replace(tzinfo=None)


def test_historical_day_stays_finalized_during_live_refresh(monkeypatch):
    """A past day with a finalized (eod) snapshot is NOT marked live and does NOT
    pick up today's live value — the live path only touches today's cell."""
    db, user = _setup(monkeypatch, Decimal("-150.72"))
    past = date(2026, 9, 15)  # a past Monday, well before "today"
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=user.id, day=past, realized_pnl=Decimal("42"),
        pct=Decimal("0.5"), trade_count=0, source="marked", snapshot_type="eod",
        eod_unrealized=Decimal("-10"), hidden=False,
    ))
    db.flush()
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                               tz="America/New_York", user_id=None)
    todc = next(r for r in rows if r.day == today)
    pastc = next(r for r in rows if r.day == past)
    # Today: live broker value.
    assert todc.live is True and todc.day_pnl == Decimal("-150.72")
    assert todc.source == "webull_live"
    # Past: finalized from the eod snapshot, NOT live, NOT the live number/source.
    assert pastc.live is False
    assert pastc.day_pnl == Decimal("42")
    assert pastc.day_pnl_pct == Decimal("0.5")
    assert pastc.source == "broker_reported"


def _filled(db, acct, side, qty, price, when):
    o = Order(
        id=uuid.uuid4(), user_id=acct.user_id, broker_account_id=acct.id,
        instrument_type=InstrumentType.STOCK, symbol="AAA", side=side,
        order_type=OrderType.MARKET, quantity=Decimal(str(qty)), status=OrderStatus.FILLED,
        filled_quantity=Decimal(str(qty)), filled_avg_price=Decimal(str(price)),
        created_at=when, closed_at=when,
    )
    db.add(o)
    db.flush()


def _pin_cutover(monkeypatch, d: date):
    """Force the authoritative-history cutover to ``d`` regardless of env —
    calendar_pnl only reads .authoritative_history_start off the settings."""
    monkeypatch.setattr(
        trades, "get_settings",
        lambda: types.SimpleNamespace(authoritative_history_start=d),
    )


def test_webull_historical_legacy_fallback(monkeypatch):
    """A Webull historical day from BEFORE the cutover, with NO finalized broker
    figure but with trades, falls back to the exact number the OLD calendar
    showed in its bold headline — "Marked" = realized + the day's unrealized
    swing — flagged legacy_calculated / estimated, restoring visibility without
    claiming authority. (Here the position closes same-day so marked == realized
    == +10.)"""
    db, user = _setup(monkeypatch, Decimal("0"))
    acct = db.query(BrokerAccount).filter(BrokerAccount.user_id == user.id).first()
    _pin_cutover(monkeypatch, date(2026, 10, 1))
    past = date(2026, 9, 15)  # a past Monday, BEFORE the cutover
    t = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    _filled(db, acct, OrderSide.BUY, 1, 100, t)
    _filled(db, acct, OrderSide.SELL, 1, 110, t)  # realizes +10
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                               tz="America/New_York", user_id=None)
    cell = next(r for r in rows if r.day == past)
    assert cell.day_pnl == Decimal("10"), "legacy value = old 'Marked' headline"
    assert cell.source == "legacy_calculated"
    assert cell.quality == "estimated"
    assert cell.day_pnl_pct is None, "no fabricated % for legacy days"
    # A real finalized EOD snapshot still WINS over legacy.
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=user.id, day=past, realized_pnl=Decimal("99"),
        pct=Decimal("1.5"), trade_count=0, source="marked", snapshot_type="eod",
        hidden=False,
    ))
    db.flush()
    rows2 = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                                tz="America/New_York", user_id=None)
    cell2 = next(r for r in rows2 if r.day == past)
    assert cell2.day_pnl == Decimal("99") and cell2.source == "broker_reported", \
        "finalized EOD snapshot overrides legacy"


def test_after_cutover_missing_eod_is_unavailable_not_legacy(monkeypatch):
    """REGRESSION: a settled day ON/AFTER the authoritative-EOD cutover, with
    trades but NO finalized EOD snapshot, is a genuine gap (outage / missed
    finalization). It MUST read '--' (unavailable) and MUST NOT silently fall
    back to the FIFO/legacy reconstruction — otherwise a future missed snapshot
    would masquerade as real history."""
    db, user = _setup(monkeypatch, Decimal("0"))
    acct = db.query(BrokerAccount).filter(BrokerAccount.user_id == user.id).first()
    # Pin the cutover BEFORE the test day so the day counts as "after cutover".
    _pin_cutover(monkeypatch, date(2026, 9, 1))
    past = date(2026, 9, 15)  # >= cutover(2026-09-01), still a past day
    t = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
    _filled(db, acct, OrderSide.BUY, 1, 100, t)
    _filled(db, acct, OrderSide.SELL, 1, 110, t)  # realizes +10 — would be legacy pre-cutover
    today = market_hours.now_et().date()
    rows = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                               tz="America/New_York", user_id=None)
    cell = next(r for r in rows if r.day == past)
    assert cell.day_pnl is None, "missing EOD after cutover must be '--', not FIFO"
    assert cell.source == "none"
    assert cell.quality == "unavailable"
    # And a real finalized EOD snapshot for that same after-cutover day still works.
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=user.id, day=past, realized_pnl=Decimal("55"),
        pct=Decimal("0.9"), trade_count=0, source="marked", snapshot_type="eod",
        hidden=False,
    ))
    db.flush()
    rows2 = trades.calendar_pnl(db=db, user=user, from_=past, to=today,
                                tz="America/New_York", user_id=None)
    cell2 = next(r for r in rows2 if r.day == past)
    assert cell2.day_pnl == Decimal("55") and cell2.source == "broker_reported", \
        "a real EOD snapshot after cutover resolves authoritative"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
