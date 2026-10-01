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
