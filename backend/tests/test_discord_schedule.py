"""Tests for a Discord source's active window.

The failure that matters here is asymmetric: watching when you didn't need to
costs memory, but NOT watching when you should silently drops trade alerts. So
every ambiguous or misconfigured case must fail OPEN (keep watching), and these
tests pin that.
"""
import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_schedule as sch

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# 2026-09-10 is a Thursday; 2026-09-12 a Saturday.
WEEKDAY_1000 = et(2026, 9, 10, 10, 0)
WEEKDAY_0300 = et(2026, 9, 10, 3, 0)
WEEKDAY_1800 = et(2026, 9, 10, 18, 0)
SATURDAY_1000 = et(2026, 9, 12, 10, 0)


# ── always ───────────────────────────────────────────────────────────────────

def test_always_is_always_on():
    assert sch.in_window(mode=sch.ALWAYS, now=WEEKDAY_0300)
    assert sch.in_window(mode=sch.ALWAYS, now=SATURDAY_1000)


def test_missing_mode_defaults_to_always():
    """Sources created before this feature have no mode; they must not go dark."""
    assert sch.in_window(mode=None, now=WEEKDAY_0300)


# ── market / extended ────────────────────────────────────────────────────────

def test_market_hours_covers_the_regular_session_only():
    assert sch.in_window(mode=sch.MARKET, now=WEEKDAY_1000)
    assert not sch.in_window(mode=sch.MARKET, now=WEEKDAY_0300)   # pre-market
    assert not sch.in_window(mode=sch.MARKET, now=WEEKDAY_1800)   # post-market


def test_market_hours_is_off_at_the_weekend():
    assert not sch.in_window(mode=sch.MARKET, now=SATURDAY_1000)


def test_extended_hours_spans_pre_regular_and_post():
    """The regular session sits BETWEEN pre and post, so it must be included —
    a naive pre-or-post check would go dark from 09:30 to 16:00."""
    assert sch.in_window(mode=sch.EXTENDED, now=et(2026, 9, 10, 5))   # pre-market
    assert sch.in_window(mode=sch.EXTENDED, now=WEEKDAY_1000)     # regular session
    assert sch.in_window(mode=sch.EXTENDED, now=WEEKDAY_1800)     # post-market


def test_extended_hours_excludes_the_true_overnight():
    # Pre-market opens at 04:00, so 03:00 is outside even the extended window.
    assert not sch.in_window(mode=sch.EXTENDED, now=WEEKDAY_0300)
    assert not sch.in_window(mode=sch.EXTENDED, now=et(2026, 9, 10, 22))
    assert not sch.in_window(mode=sch.EXTENDED, now=SATURDAY_1000)


# ── custom ───────────────────────────────────────────────────────────────────

def test_custom_window_within_a_day():
    kw = dict(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0), timezone="America/New_York")
    assert sch.in_window(**kw, now=WEEKDAY_1000)
    assert not sch.in_window(**kw, now=WEEKDAY_0300)
    assert not sch.in_window(**kw, now=WEEKDAY_1800)


def test_custom_window_end_is_exclusive():
    kw = dict(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0), timezone="America/New_York")
    assert sch.in_window(**kw, now=et(2026, 9, 10, 16, 59))
    assert not sch.in_window(**kw, now=et(2026, 9, 10, 17, 0))


def test_custom_window_crossing_midnight():
    """22:00–06:00 must cover both the late evening and the small hours."""
    kw = dict(mode=sch.CUSTOM, start=time(22, 0), end=time(6, 0),
              timezone="America/New_York", days=[0, 1, 2, 3, 4])
    assert sch.in_window(**kw, now=et(2026, 9, 10, 23))       # Thu night
    assert sch.in_window(**kw, now=et(2026, 9, 11, 2))        # Fri 02:00, Thu's window
    assert not sch.in_window(**kw, now=et(2026, 9, 10, 12))   # midday


def test_a_midnight_crossing_window_attributes_the_tail_to_the_start_day():
    """A Fri-night window running to 06:00 Sat must still count, even though
    Saturday isn't in the allowed days."""
    kw = dict(mode=sch.CUSTOM, start=time(22, 0), end=time(6, 0),
              timezone="America/New_York", days=[4])          # Friday only
    assert sch.in_window(**kw, now=et(2026, 9, 11, 23))       # Fri 23:00
    assert sch.in_window(**kw, now=et(2026, 9, 12, 3))        # Sat 03:00 = Fri's window
    assert not sch.in_window(**kw, now=et(2026, 9, 12, 23))   # Sat night: not allowed


def test_custom_days_restrict_the_window():
    kw = dict(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0),
              timezone="America/New_York", days=[0, 1])       # Mon, Tue only
    assert not sch.in_window(**kw, now=WEEKDAY_1000)          # Thursday
    assert sch.in_window(**kw, now=et(2026, 9, 7, 10))        # Monday


def test_custom_defaults_to_weekdays_when_no_days_given():
    kw = dict(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0), timezone="America/New_York")
    assert sch.in_window(**kw, now=WEEKDAY_1000)
    assert not sch.in_window(**kw, now=SATURDAY_1000)


def test_custom_respects_its_own_timezone():
    """10:00 ET is 15:00 London, so a London 09:00-17:00 window is open."""
    kw = dict(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0), timezone="Europe/London")
    assert sch.in_window(**kw, now=WEEKDAY_1000)                # 15:00 London
    # 03:00 ET = 08:00 London — before the window opens.
    assert not sch.in_window(**kw, now=WEEKDAY_0300)


# ── fail-open behaviour ──────────────────────────────────────────────────────

def test_an_incomplete_custom_window_keeps_watching():
    """Half-configured must not mean "never" — that would silently drop alerts."""
    assert sch.in_window(mode=sch.CUSTOM, start=time(9, 0), end=None)
    assert sch.in_window(mode=sch.CUSTOM, start=None, end=time(17, 0))


def test_a_zero_length_window_is_read_as_all_day():
    assert sch.in_window(mode=sch.CUSTOM, start=time(9, 0), end=time(9, 0),
                         timezone="America/New_York", now=WEEKDAY_1000)


def test_an_unknown_timezone_falls_back_instead_of_going_dark():
    """A typo in the timezone must not take the source offline permanently."""
    assert sch.in_window(mode=sch.CUSTOM, start=time(9, 0), end=time(17, 0),
                         timezone="Not/AZone", now=WEEKDAY_1000)


def test_an_unknown_mode_keeps_watching():
    assert sch.in_window(mode="whatever", now=WEEKDAY_0300)


# ── description ──────────────────────────────────────────────────────────────

def test_describe_renders_each_mode():
    assert sch.describe(mode=sch.ALWAYS) == "Always"
    assert "09:30" in sch.describe(mode=sch.MARKET)
    assert "04:00" in sch.describe(mode=sch.EXTENDED)
    assert sch.describe(
        mode=sch.CUSTOM, start=time(9, 0), end=time(17, 30), timezone="Europe/London"
    ) == "09:00–17:30 Europe/London"
