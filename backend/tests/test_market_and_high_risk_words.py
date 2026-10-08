"""Words in an alert that change how an ENTRY is filled or sized, any channel.

    "out the gate", "@market", "@ market"  -> fill at market
    "high risk", "high-risk", "risky"      -> half size, like "light"
"""
from datetime import datetime, timezone

import pytest

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage, ParseStatus, SignalAction

AT = datetime(2026, 10, 8, 13, 35, tzinfo=timezone.utc)


def _entry(text):
    r = parse_message(ParsedMessage(content=text, posted_at=AT))
    assert r.status is ParseStatus.PARSED, r
    s = r.signals[0]
    assert s.action is SignalAction.BUY
    return s


def test_the_breakdown_sniper_call_is_a_half_size_entry_at_market():
    s = _entry("QQQ 759P @here @everyone out the gate high risk")
    assert (s.symbol, s.half_size, s.at_market) == ("QQQ", True, True)
    assert s.as_dict()["at_market"] is True


@pytest.mark.parametrize("text", [
    "$SPY 771 CALL 0DTE @0.68 @market",
    "$SPY 771 CALL 0DTE @0.68 @ Market",
    "$SPY 771 CALL 0DTE @0.68 out of the gate",
])
def test_market_words_fill_at_market_and_keep_the_price(text):
    s = _entry(text)
    assert s.at_market is True and str(s.limit_price) == "0.68"   # the price still sizes and caps


@pytest.mark.parametrize("text", ["QQQ 759P @here .42 high-risk", "QQQ 759P @here .42 high risk",
                                  "QQQ 759P @here .42 risky"])
def test_high_risk_is_half_size(text):
    assert _entry(text).half_size is True


def test_a_plain_entry_is_neither():
    s = _entry("$SPY 771 CALL 0DTE @0.68")
    assert s.at_market is False and s.half_size is False


def test_an_exit_is_never_flagged():
    r = parse_message(ParsedMessage(content="TSLA -90% Out @market", posted_at=AT))
    assert r.signals[0].action is SignalAction.SELL and r.signals[0].at_market is False
