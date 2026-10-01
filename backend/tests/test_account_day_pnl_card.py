"""The top 'Day's P&L' card uses the broker-aware ACCOUNT Day P&L (same as the
calendar today cell), NOT FIFO realized. Backed by GET /api/positions/day-pnl,
which reuses the calendar's live resolver.
"""
import datetime
import os
import sys
import types
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import positions, trades


def _call(monkeypatch, live_ret, snap_ret=None):
    # The endpoint does a local `from app.api.trades import _live_day_pnl_today`,
    # so patching the attribute on the trades module takes effect.
    monkeypatch.setattr(trades, "_live_day_pnl_today", lambda db, uid: live_ret)
    monkeypatch.setattr(trades, "_last_marked_snapshot", lambda db, uid, day: snap_ret)
    db = Session(create_engine("sqlite:///:memory:"))
    user = types.SimpleNamespace(id=uuid.uuid4())
    return positions.account_day_pnl(db=db, user=user)


def test_alpaca_card_shows_broker_day_pnl_not_fifo(monkeypatch):
    # Broker/account Day P&L = -3043.94; FIFO realized would be 0.00.
    r = _call(monkeypatch, (Decimal("-3043.94"), Decimal("-2.2003"), "alpaca_live"))
    assert r["day_pnl"] == -3043.94, "must be broker account Day P&L, not FIFO 0.00"
    assert r["source"] == "alpaca_live"
    assert r["quality"] == "authoritative"


def test_webull_card(monkeypatch):
    r = _call(monkeypatch, (Decimal("-198.26"), Decimal("-0.0198"), "webull_live"))
    assert r["day_pnl"] == -198.26
    assert r["source"] == "webull_live"


def test_zero_is_not_unavailable(monkeypatch):
    r = _call(monkeypatch, (Decimal("0"), Decimal("0"), "webull_live"))
    assert r["day_pnl"] == 0.0, "a genuine broker 0.00 must stay 0.00, not '--'"
    assert r["quality"] == "authoritative"


def test_unavailable_when_no_live_broker(monkeypatch):
    r = _call(monkeypatch, None)
    assert r["day_pnl"] is None
    assert r["quality"] == "unavailable"


def test_stale_fallback_mirrors_calendar(monkeypatch):
    # Live fetch failed (None, None, source) → last-known broker value, stale.
    r = _call(monkeypatch, (None, None, "webull_live"),
              snap_ret=(Decimal("-140.00"), Decimal("-1.62"), datetime.datetime.now()))
    assert r["day_pnl"] == -140.0
    assert r["source"] == "webull_live"
    assert r["quality"] == "stale"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
