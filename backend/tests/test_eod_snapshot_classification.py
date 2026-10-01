"""EOD snapshot classification safety.

A broker MARKED snapshot may be tagged snapshot_type='eod' ONLY after the
official regular-session close (16:00 ET) on a real trading day — never
pre-market, never on a weekend or market holiday. This guards the premarket
bug: 08:00 ET is "not in regular session" but must NOT be finalized.
"""
import os
import sys
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import market_hours as mh

ET = mh.ET
WED = (2026, 9, 30)   # a regular trading Wednesday
SAT = (2026, 10, 3)   # weekend


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# ── the classification the snapshot job uses: eod ⇔ past_regular_close ────────

def test_premarket_8am_is_not_eod():
    assert mh.past_regular_close(_at(*WED, 8, 0)) is False

def test_10am_intraday_not_eod():
    assert mh.in_regular_session(_at(*WED, 10, 0)) is True
    assert mh.past_regular_close(_at(*WED, 10, 0)) is False

def test_359pm_intraday_not_eod():
    assert mh.past_regular_close(_at(*WED, 15, 59)) is False

def test_after_close_is_eod_eligible():
    assert mh.past_regular_close(_at(*WED, 16, 1)) is True
    assert mh.past_regular_close(_at(*WED, 20, 0)) is True

def test_weekend_after_close_is_not_eod():
    assert mh.past_regular_close(_at(*SAT, 17, 0)) is False

def test_holiday_after_close_is_not_eod():
    # Christmas 2026-12-25 (Friday) — a full-day market holiday.
    assert mh.is_market_holiday(date(2026, 12, 25)) is True
    assert mh.past_regular_close(_at(2026, 12, 25, 17, 0)) is False


# ── holiday calendar spot-checks ─────────────────────────────────────────────

def test_known_market_holidays_2026():
    holidays = [
        date(2026, 1, 1),    # New Year's Day (Thu)
        date(2026, 1, 19),   # MLK — 3rd Mon Jan
        date(2026, 2, 16),   # Presidents' — 3rd Mon Feb
        date(2026, 4, 3),    # Good Friday (Easter 2026 = Apr 5)
        date(2026, 5, 25),   # Memorial — last Mon May
        date(2026, 6, 19),   # Juneteenth (Fri)
        date(2026, 7, 3),    # Independence observed (Jul 4 is Sat)
        date(2026, 9, 7),    # Labor — 1st Mon Sep
        date(2026, 11, 26),  # Thanksgiving — 4th Thu Nov
        date(2026, 12, 25),  # Christmas (Fri)
    ]
    for d in holidays:
        assert mh.is_market_holiday(d) is True, d
    # A plain trading Wednesday is not a holiday.
    assert mh.is_market_holiday(date(2026, 1, 7)) is False
    assert mh.is_regular_trading_day(_at(2026, 1, 7, 12, 0)) is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
