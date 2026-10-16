"""Market-session classification + market-hours-aware polling cadence.

Background pollers run at full cadence during tradable hours (pre-market,
regular, after-hours on a real trading day) and back off when the market is
CLOSED (overnight, weekends, holidays). Copy-trading/risk cadence during
tradable hours is unchanged.

All tests use frozen ET timestamps — no dependence on today's date, the machine
timezone, the live market state, or the network.
"""
import os
import sys
import uuid
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models.broker_account import BrokerName
from app.services import market_hours as mh, pnl_poller, webull_listener

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


# ─────────────────────────── market_session ───────────────────────────
# 2026-10-06 is a normal Tuesday; 2026-10-10/11 are Sat/Sun.
def test_session_overnight_before_premarket():
    assert mh.market_session(_et(2026, 10, 6, 3, 0)) == mh.CLOSED


def test_session_pre_market():
    assert mh.market_session(_et(2026, 10, 6, 5, 0)) == mh.PRE_MARKET


def test_session_regular():
    assert mh.market_session(_et(2026, 10, 6, 10, 0)) == mh.REGULAR


def test_session_after_hours():
    assert mh.market_session(_et(2026, 10, 6, 18, 0)) == mh.AFTER_HOURS


def test_session_overnight_after_postmarket():
    assert mh.market_session(_et(2026, 10, 6, 21, 0)) == mh.CLOSED


def test_session_saturday_and_sunday():
    assert mh.market_session(_et(2026, 10, 10, 11, 0)) == mh.CLOSED  # Sat
    assert mh.market_session(_et(2026, 10, 11, 11, 0)) == mh.CLOSED  # Sun


def test_session_us_holiday_on_a_weekday():
    # MLK Day 2026-01-19 (Mon), Thanksgiving 2026-11-26 (Thu), Christmas 2026-12-25 (Fri).
    assert mh.market_session(_et(2026, 1, 19, 11, 0)) == mh.CLOSED
    assert mh.market_session(_et(2026, 11, 26, 11, 0)) == mh.CLOSED
    assert mh.market_session(_et(2026, 12, 25, 11, 0)) == mh.CLOSED


def test_session_observed_holiday_is_closed():
    # An OBSERVED holiday (shifted off the literal date) is CLOSED all day, both
    # shift directions: Independence Day 2026-07-04 is a Saturday → observed the
    # prior Friday (07-03); 2027-07-04 is a Sunday → observed the next Monday
    # (07-05). Tested directly on market_session (not just is_market_holiday).
    assert mh.is_market_holiday(date(2026, 7, 3)) is True
    assert mh.market_session(_et(2026, 7, 3, 11, 0)) == mh.CLOSED   # Sat → prior Fri
    assert mh.is_market_holiday(date(2027, 7, 5)) is True
    assert mh.market_session(_et(2027, 7, 5, 11, 0)) == mh.CLOSED   # Sun → next Mon


def test_session_early_close_day_before_and_after_close():
    # Black Friday 2026-11-27 is a half day: the REGULAR session ends early at
    # 13:00, but AFTER_HOURS continues to 20:00 ET (same boundary as
    # in_extended_hours), so polling stays at full cadence while the app still
    # considers extended-hours trading active.
    assert mh.is_early_close_day(date(2026, 11, 27)) is True
    assert mh.market_session(_et(2026, 11, 27, 12, 59)) == mh.REGULAR      # before the early close
    assert mh.market_session(_et(2026, 11, 27, 13, 1)) == mh.AFTER_HOURS   # just after 13:00
    assert mh.market_session(_et(2026, 11, 27, 17, 0)) == mh.AFTER_HOURS   # still extended hours
    assert mh.market_session(_et(2026, 11, 27, 18, 0)) == mh.AFTER_HOURS
    assert mh.market_session(_et(2026, 11, 27, 19, 59)) == mh.AFTER_HOURS  # last extended-hours minute
    assert mh.market_session(_et(2026, 11, 27, 20, 0)) == mh.CLOSED        # 20:00 → closed
    # A normal day at 14:00 is still REGULAR (contrast with the half day).
    assert mh.market_session(_et(2026, 11, 24, 14, 0)) == mh.REGULAR


def test_early_close_calendar():
    assert mh.is_early_close_day(date(2026, 12, 24)) is True   # Christmas Eve (Thu)
    assert mh.is_early_close_day(date(2025, 7, 3)) is True     # before Fri Jul 4 2025
    assert mh.is_early_close_day(date(2026, 7, 3)) is False    # Jul 4 2026 is Sat → Jul 3 is the holiday
    assert mh.is_early_close_day(date(2026, 11, 24)) is False  # a normal Tuesday
    assert mh.is_early_close_day(date(2026, 11, 26)) is False  # Thanksgiving is a full holiday


def test_dst_regular_session_both_sides():
    # EDT (summer) and EST (winter) both map 10:00 ET to REGULAR.
    assert mh.market_session(_et(2025, 3, 10, 10, 0)) == mh.REGULAR   # EDT
    assert mh.market_session(_et(2025, 12, 15, 10, 0)) == mh.REGULAR  # EST


def test_is_tradable_now_matches_session():
    for dt, tradable in [
        (_et(2026, 10, 6, 5, 0), True),    # pre
        (_et(2026, 10, 6, 10, 0), True),   # regular
        (_et(2026, 10, 6, 18, 0), True),   # after
        (_et(2026, 10, 6, 2, 0), False),   # overnight
        (_et(2026, 10, 10, 11, 0), False), # Sat
        (_et(2026, 12, 25, 11, 0), False), # holiday
    ]:
        assert mh.is_tradable_now(dt) is tradable


# ─────────────────────────── P&L poller cadence ───────────────────────────
def test_pnl_full_cadence_when_tradable(monkeypatch):
    monkeypatch.setattr(mh, "is_tradable_now", lambda *a, **k: True)
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == 10.0   # Alpaca REST
    assert pnl_poller._interval_for_broker(BrokerName.WEBULL) == 15.0   # Webull P&L


def test_pnl_backs_off_when_closed(monkeypatch):
    monkeypatch.setattr(mh, "is_tradable_now", lambda *a, **k: False)
    closed = float(get_settings().pnl_poll_interval_closed_seconds)
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == closed  # ~180s
    assert pnl_poller._interval_for_broker(BrokerName.WEBULL) == closed  # ~180s


def test_pnl_closed_interval_floors_bad_config(monkeypatch):
    import types as _t
    monkeypatch.setattr(pnl_poller, "get_settings",
                        lambda: _t.SimpleNamespace(pnl_poll_interval_closed_seconds=0), raising=False)
    # 0 is invalid → falls back to the 180s default, never a tight loop.
    assert pnl_poller._closed_pnl_interval() >= pnl_poller._MIN_PNL_CLOSED_S


# ─────────────────────────── Webull order-poll cadence ───────────────────────────
def test_webull_full_cadence_when_tradable(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "market_session", lambda *a, **k: mh.REGULAR)
    interval, session, active = webull_listener._poll_plan(1, uuid.uuid4())
    assert session == mh.REGULAR and active is False
    assert interval == webull_listener._safe_poll_interval(1)


def test_webull_slow_when_closed_idle(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "market_session", lambda *a, **k: mh.CLOSED)
    monkeypatch.setattr(webull_listener, "_has_working_orders", lambda uid: False)
    interval, session, active = webull_listener._poll_plan(1, uuid.uuid4())
    assert session == mh.CLOSED and active is False
    assert interval == float(get_settings().webull_poll_interval_closed_seconds)   # ~120s


def test_webull_faster_when_closed_with_working_orders(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "market_session", lambda *a, **k: mh.CLOSED)
    monkeypatch.setattr(webull_listener, "_has_working_orders", lambda uid: True)
    interval, session, active = webull_listener._poll_plan(1, uuid.uuid4())
    assert session == mh.CLOSED and active is True
    assert interval == float(get_settings().webull_poll_interval_closed_active_seconds)  # ~20s
    assert interval < float(get_settings().webull_poll_interval_closed_seconds)
    assert interval >= webull_listener._safe_poll_interval(1)   # never below the rate floor


def test_webull_tradable_ignores_working_orders(monkeypatch):
    # During tradable hours the cadence is the normal one regardless of orders.
    monkeypatch.setattr(webull_listener.market_hours, "market_session", lambda *a, **k: mh.AFTER_HOURS)
    monkeypatch.setattr(webull_listener, "_has_working_orders", lambda uid: True)
    interval, session, active = webull_listener._poll_plan(1, uuid.uuid4())
    assert session == mh.AFTER_HOURS and active is False
    assert interval == webull_listener._safe_poll_interval(1)


def test_webull_closed_interval_floors_bad_config():
    # 0 / negative / invalid never collapse the poll; floored to >= _MIN_CLOSED_POLL_S.
    assert webull_listener._sane_closed_interval(0, 120.0) == 120.0
    assert webull_listener._sane_closed_interval(-5, 120.0) == 120.0
    assert webull_listener._sane_closed_interval("bad", 120.0) == 120.0
    assert webull_listener._sane_closed_interval(2, 120.0) == webull_listener._MIN_CLOSED_POLL_S


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
