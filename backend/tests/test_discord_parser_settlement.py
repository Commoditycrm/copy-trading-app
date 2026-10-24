"""An index expiry written with its settlement ("10/16pm") is read, and "pm"
picks the PM-settled root.

WHY THIS EXISTS
---------------
Rafael posted "SPX 7750c 10/16pm  @14.2" (QA, 2026-10-14 15:11 ET). The "pm"
glued to the date stopped every pattern from matching, so a real alert was
filed as "not a trade alert" and never reached the review queue. Written
"10/16 pm @14.2" it matched, but the price was lost.

The suffix is not noise. 10/16/2026 is the third Friday, when SPX lists two
7750 calls: the AM-settled monthly (SPX) and the PM-settled weekly (SPXW).
Orders go out under the root the alert names, so dropping "pm" would buy the
monthly, which stops trading a day earlier.
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage, ParseStatus, SignalAction

TS = datetime(2026, 10, 14, 19, 11, 38, tzinfo=timezone.utc)


def _one(text):
    r = parse_message(ParsedMessage(content=text, channel="🌟｜rafael", posted_at=TS))
    assert r.status is ParseStatus.PARSED, (text, r.reason)
    return r.signals[0] if getattr(r, "signals", None) else r.signal


@pytest.mark.parametrize("text", [
    "@Rafael SPX 7750c 10/16pm  @14.2",   # verbatim
    "@Rafael SPX 7750c 10/16 pm @14.2",
    "SPX 7750c 10/16PM @14.2",
    "SPX 10/16pm 7750c @14.2",            # expiry-first house style
])
def test_rafaels_alert_is_a_limit_buy_of_the_pm_settled_weekly(text):
    s = _one(text)
    assert s.action is SignalAction.BUY
    assert s.symbol == "SPXW"
    assert s.strike == Decimal("7750")
    assert s.expiration == date(2026, 10, 16)
    assert s.limit_price == Decimal("14.2")


@pytest.mark.parametrize("text", ["SPX 7750c 10/16am @14.2", "SPX 7750c 10/16 @14.2"])
def test_am_or_no_suffix_keeps_the_root(text):
    assert _one(text).symbol == "SPX"


@pytest.mark.parametrize("text,root", [
    ("NDX 25000c 10/16pm @30", "NDXP"),
    ("RUT 2500p 10/16pm @5", "RUTW"),
    ("AAPL 330c 10/16pm @1", "AAPL"),     # not an index: suffix read, root unchanged
])
def test_pm_maps_only_index_roots(text, root):
    assert _one(text).symbol == root


def test_exits_and_adds_follow_the_same_root():
    """A trim or add must find the SPXW holding, not look for an SPX one."""
    assert _one("✂️ SPX 7750c 10/16pm +40%").symbol == "SPXW"
    assert _one("Adding SPX 7750c 10/16pm @12").symbol == "SPXW"


@pytest.mark.parametrize("text", ["SPX 7750c pm @14.2", "the 4pm close looks weak"])
def test_a_bare_pm_is_still_not_a_trade(text):
    r = parse_message(ParsedMessage(content=text, channel="🌟｜rafael", posted_at=TS))
    assert r.status is ParseStatus.IGNORED
