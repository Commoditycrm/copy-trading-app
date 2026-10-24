""""2DTE", "5DTE" … name an expiry that many TRADING days after the alert.

Mark, 2026-10-19: "NVDA 220C 2DTE @2.01 Risky @Mark" was ignored as "not a
trade alert" — only 0DTE / ODTE / 1DTE were read.
"""
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage

MON = datetime(2026, 10, 19, 15, 45, tzinfo=timezone.utc)
FRI = datetime(2026, 10, 16, 15, 45, tzinfo=timezone.utc)


def _sig(text, at=MON):
    r = parse_message(ParsedMessage(content=text, posted_at=at))
    assert r.signals, r
    return r.signals[0]


def test_marks_2dte_call_is_read():
    s = _sig("NVDA 220C 2DTE @2.01 Risky @Mark")
    assert (s.action.value, s.symbol, str(s.strike), s.option_type.value) == ("BUY", "NVDA", "220", "CALL")
    assert s.expiration == date(2026, 10, 21) and str(s.limit_price) == "2.01" and s.half_size


@pytest.mark.parametrize("text, at, expiry", [
    ("NVDA 220C 2DTE @2.01", FRI, date(2026, 10, 20)),      # Friday + 2 trading days = Tuesday
    ("$SPY 770 CALL 5DTE @1.10", MON, date(2026, 10, 26)),
    ("NVDA 220C 1DTE @2.01", FRI, date(2026, 10, 19)),      # unchanged: Friday 1DTE is Monday
    ("NVDA 220C 0DTE @2.01", MON, date(2026, 10, 19)),
    ("NVDA 220C ODTE @2.01", MON, date(2026, 10, 19)),
])
def test_ndte_counts_trading_days(text, at, expiry):
    assert _sig(text, at).expiration == expiry


def test_too_far_out_is_refused_by_name():
    r = parse_message(ParsedMessage(content="NVDA 220C 45DTE @2.01", posted_at=MON))
    assert r.status.value == "invalid" and "45DTE" in r.reason
