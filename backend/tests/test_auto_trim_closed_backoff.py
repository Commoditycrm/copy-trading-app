"""Discord auto-trim backs off its sweep cadence when the market is CLOSED.

Auto-trim only takes profit (fires ladder/AI trims) and sets on-fill stops; no
trim can fill while the market is shut, so the sweep slows from 15s to ~180s
when CLOSED. Tradable cadence (pre-market / regular / after-hours, incl. the
half-day window to 20:00 ET) is unchanged. Frozen ET clock — no dependence on
today's date, the machine TZ, the live market, or the network.
"""
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo

import app.config as cfg
from app.services import discord_auto_trim as at
from app.services import market_hours as mh

ET = ZoneInfo("America/New_York")


def _freeze(monkeypatch, y, mo, d, h, mi=0):
    monkeypatch.setattr(mh, "now_et", lambda: datetime(y, mo, d, h, mi, tzinfo=ET))


# ─────────────────────────── tradable = 15s unchanged ───────────────────────────
# 2026-10-06 is a normal Tuesday.
def test_pre_market_keeps_15s(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 6, 5, 0)
    interval, session = at._interval_and_session()
    assert session == mh.PRE_MARKET and interval == 15.0


def test_regular_keeps_15s(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 6, 10, 0)
    interval, session = at._interval_and_session()
    assert session == mh.REGULAR and interval == 15.0


def test_after_hours_keeps_15s(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 6, 18, 0)
    interval, session = at._interval_and_session()
    assert session == mh.AFTER_HOURS and interval == 15.0


# ─────────────────────────── CLOSED = backed off ───────────────────────────
def test_overnight_backs_off(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 6, 2, 0)
    interval, session = at._interval_and_session()
    assert session == mh.CLOSED and interval == 180.0


def test_after_2000_backs_off(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 6, 20, 1)
    interval, session = at._interval_and_session()
    assert session == mh.CLOSED and interval == 180.0


def test_weekend_backs_off(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 10, 11, 0)   # Saturday
    assert at._interval_and_session() == (180.0, mh.CLOSED)
    _freeze(monkeypatch, 2026, 10, 11, 11, 0)   # Sunday
    assert at._interval_and_session() == (180.0, mh.CLOSED)


def test_holiday_backs_off(monkeypatch):
    _freeze(monkeypatch, 2026, 12, 25, 11, 0)   # Christmas (Fri)
    assert at._interval_and_session() == (180.0, mh.CLOSED)


# ─────────────────────────── early-close day ───────────────────────────
def test_early_close_before_2000_keeps_15s(monkeypatch):
    # Black Friday 2026-11-27: after-hours runs to 20:00 ET (Option B), so a
    # trim can still fill — keep 15s until 20:00.
    _freeze(monkeypatch, 2026, 11, 27, 18, 0)
    interval, session = at._interval_and_session()
    assert session == mh.AFTER_HOURS and interval == 15.0


def test_early_close_after_2000_backs_off(monkeypatch):
    _freeze(monkeypatch, 2026, 11, 27, 20, 30)
    interval, session = at._interval_and_session()
    assert session == mh.CLOSED and interval == 180.0


# ─────────────────────────── config safety ───────────────────────────
def test_closed_interval_floors_bad_config(monkeypatch):
    # 0 / negative / non-numeric never speed the loop or spin it — fall back to
    # the slow default and never below the tradable 15s floor.
    for bad in (0, -5, "nope", None):
        monkeypatch.setattr(
            cfg, "get_settings",
            lambda b=bad: types.SimpleNamespace(discord_auto_trim_closed_interval_seconds=b),
        )
        assert at._closed_interval() >= at._MIN_CLOSED_INTERVAL_S
        assert at._closed_interval() == 180.0


def test_closed_interval_never_faster_than_tradable(monkeypatch):
    # A too-small (but positive) value is floored up to the tradable cadence.
    monkeypatch.setattr(
        cfg, "get_settings",
        lambda: types.SimpleNamespace(discord_auto_trim_closed_interval_seconds=3),
    )
    assert at._closed_interval() == float(at.POLL_INTERVAL_S)


def test_tradable_base_interval_unchanged():
    # This PR must not change the tradable sweep cadence.
    assert at.POLL_INTERVAL_S == 15
