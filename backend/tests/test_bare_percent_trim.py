"""A bare percentage is ALWAYS a trim.

Live: Mark posts "TSLA 375C ODTE @1.79 @Mark" and then trims with
"TSLA 60% @Mark" / "TSLA 70% @Mark". Nothing was sold. The shape was gated on
the source's percent_means_exit, that setting had no UI, and so in practice it
was never on and every trim of this shape was silently dropped.

The gate is gone. The trade-off it existed for is real and is accepted: on a
channel that posts a bare percentage as running P&L while still holding, each
post now reads as a rung of the trim ladder. What still protects the position
is that the contract is resolved from what is actually HELD and the ladder
decides the size — so this can never sell something that is not there.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage, ParseStatus, SignalAction

_NOW = datetime(2026, 9, 25, 14, 30, tzinfo=timezone.utc)


def _read(text, *, pct_exit):
    return parse_message(ParsedMessage(
        content=text, author="Mark", posted_at=_NOW, percent_means_exit=pct_exit,
    ))


@pytest.mark.parametrize("text", ["TSLA 60% @Mark", "TSLA 70% @Mark"])
def test_a_bare_percent_trims_when_the_channel_says_so(text):
    r = _read(text, pct_exit=True)
    assert r.status is ParseStatus.PARSED
    s = r.signals[0]
    assert s.action is SignalAction.SELL
    assert s.symbol == "TSLA"
    # No contract in the text — it has to come from what is actually held, and
    # guessing one would sell the wrong option.
    assert s.contract_unspecified is True


@pytest.mark.parametrize("text", ["TSLA 60% @Mark", "TSLA 70% @Mark"])
def test_it_no_longer_depends_on_a_channel_setting(text):
    """The whole point of the change. percent_means_exit defaulted off and had
    no UI, so gating on it meant this shape never traded at all."""
    assert _read(text, pct_exit=False).status is ParseStatus.PARSED
    assert _read(text, pct_exit=True).status is ParseStatus.PARSED


def test_the_trailing_mention_does_not_block_it():
    """"@Mark" is a Discord ping, and _PCT_BARE_RE deliberately refuses any
    trailing prose — so the mention has to be stripped before it is matched,
    or every one of this author's trims is ignored."""
    assert _read("TSLA 60% @Mark", pct_exit=True).status is ParseStatus.PARSED


def test_the_entry_that_precedes_it_still_reads(a=None):
    """The O in "ODTE" is a letter, not a zero — as the author types it."""
    r = _read("TSLA 375C ODTE @1.79 @Mark", pct_exit=True)
    s = r.signals[0]
    assert s.action is SignalAction.BUY
    assert str(s.strike) == "375"
    assert s.expiration == _NOW.date()
    assert str(s.limit_price) == "1.79"


def test_prose_carrying_a_percentage_is_still_not_a_sell():
    """The guard that makes the strict pattern worth having: "AMD 27% of the
    float is short" must not become an order, even on a channel where a bare
    percentage does mean a trim."""
    assert _read("AMD 27% of the float is short", pct_exit=True).status is not ParseStatus.PARSED


def test_a_contract_plus_percentage_is_still_gated():
    """Deliberately NOT widened. "$TSLA 375c +43%" names the whole contract and
    is the shape channels repeat as a position runs (+24%, +45%, +60%) — three
    of those would be three sells for one position. Only the BARE form, which
    is the one Mark actually uses to trim, was ungated."""
    assert _read("$TSLA 375c +43%", pct_exit=False).status is not ParseStatus.PARSED
    assert _read("$TSLA 375c +43%", pct_exit=True).status is ParseStatus.PARSED


def test_the_contract_is_never_guessed():
    """A bare percentage names no strike, right or expiry. Execution resolves
    those from the open position; inventing them would sell the wrong option."""
    s = _read("TSLA 60% @Mark", pct_exit=False).signals[0]
    assert s.strike is None and s.option_type is None and s.expiration is None
    assert s.contract_unspecified is True
