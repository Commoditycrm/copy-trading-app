"""Free-text alerts that give a same-day expiry in words.

Live 2026-10-01 (Julia): "BTO SPY 767 Calls Today Expiry @1.46 filled" was
refused as "the alert has no expiry" — the free-text parser only read dates.
"""
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage, ParseStatus

MORNING = datetime(2026, 10, 1, 14, 5, tzinfo=timezone.utc)          # 10:05 ET


def _parse(text, posted_at=MORNING):
    return parse_message(ParsedMessage(content=text, posted_at=posted_at))


def test_julias_alert_is_a_same_day_call_entry():
    r = _parse("BTO SPY 767 Calls Today Expiry @1.46 filled")
    assert r.status is ParseStatus.PARSED
    s = r.signals[0]
    assert (s.action.value, s.symbol, str(s.strike), s.option_type.value) == ("BUY", "SPY", "767", "CALL")
    assert s.expiration == date(2026, 10, 1)
    assert str(s.limit_price) == "1.46"


@pytest.mark.parametrize("words", [
    "Today Expiry", "Today's expiry", "todays exp", "expiring today", "exp today",
    "0DTE", "ODTE", "same day", "same-day",
])
def test_same_day_wordings(words):
    r = _parse(f"BTO SPY 767 Calls {words} @1.46")
    assert r.status is ParseStatus.PARSED
    assert r.signals[0].expiration == date(2026, 10, 1)


def test_an_evening_post_uses_the_eastern_date():
    """23:30 ET is already the next day in UTC; today is still the market's day."""
    late = datetime(2026, 10, 2, 3, 30, tzinfo=timezone.utc)
    assert _parse("BTO SPY 767 Calls Today Expiry @1.46", late).signals[0].expiration == date(2026, 10, 1)


def test_an_explicit_date_still_wins():
    assert _parse("BTO SPY 767 Calls 10/03 @1.46").signals[0].expiration == date(2026, 10, 3)


def test_today_without_a_timestamp_is_refused():
    r = _parse("BTO SPY 767 Calls Today Expiry @1.46", posted_at=None)
    assert r.status is ParseStatus.INVALID


def test_no_expiry_at_all_is_still_refused():
    assert _parse("BTO SPY 767 Calls @1.46").status is ParseStatus.INVALID


def test_an_entry_with_no_stated_size_is_one_contract():
    """QA 2026-10-01: refused at execution as "The alert states no quantity"."""
    s = _parse("BTO SPY 770 Calls Today Expiry @1.38 filled").signals[0]
    assert str(s.quantity) == "1"


def test_a_stated_size_is_kept():
    assert str(_parse("BTO 3 SPY 770 Calls Today Expiry @1.38").signals[0].quantity) == "3"


def test_an_exit_stays_sized_from_the_position():
    s = _parse("STC SPY 770 Calls Today Expiry @1.60").signals[0]
    assert s.action.value == "SELL" and s.quantity is None


@pytest.mark.parametrize("words", ["filled lightly", "light", "not heavy", "small size",
                                   "smaller size", "half size", "half-size", "lotto", "LOTTO play",
                                   "lottos"])
def test_wordings_that_mean_half_size(words):
    s = _parse(f"BTO SPY 764 Calls Today Expiry @1.02 {words}").signals[0]
    assert s.half_size is True


@pytest.mark.parametrize("words", ["filled", "lighten up", "lightening", "flashlight", "delight", "size up",
                                   "lottery"])
def test_wordings_that_do_not(words):
    s = _parse(f"BTO SPY 764 Calls Today Expiry @1.02 {words}").signals[0]
    assert s.half_size is False



@pytest.mark.parametrize("words", ["Tommorow Expiry", "Tomorrow Expiry", "tomorrow's expiry",
                                   "Tomorow exp", "Tommorrow", "tmrw", "1DTE", "expiring tomorrow"])
def test_tomorrow_wordings_are_the_next_trading_day(words):
    s = _parse(f"BTO SPY 764 Calls {words} @2.41 filled lightly").signals[0]
    assert s.expiration == date(2026, 10, 2)          # posted Thu 2026-10-01
    assert s.half_size is True


def test_a_friday_tomorrow_is_monday():
    friday = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    assert _parse("BTO SPY 764 Calls Tomorrow Expiry @2.41", friday).signals[0].expiration == date(2026, 10, 5)
