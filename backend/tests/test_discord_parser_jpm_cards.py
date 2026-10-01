"""JPM (ALTORI) cards: the title is the action, the description the contract.

Live 2026-09-30 these were read by contract alone, so all three came out as a
BUY — the "Update" added to the position instead of trimming it, and "Close"
would have bought again.
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import OptionType, ParsedMessage, ParseStatus, SignalAction

TS = datetime(2026, 9, 30, 12, 38, tzinfo=timezone.utc)
FOOTER = "Jpm Options | For Informational Purposes Only – Not Financial Advice • Today at 8:38 AM"


def _card(title, desc, **kw):
    r = parse_message(ParsedMessage(
        content="@JPM", embeds=[{"title": title, "description": desc, "footer": FOOTER}],
        author="JPM (ALTORI)", posted_at=TS, **kw,
    ))
    assert r.status is ParseStatus.PARSED, r
    return r.signals[0] if getattr(r, "signals", None) else r.signal


def test_open_is_a_limit_buy_of_the_contract():
    s = _card("Open", "SPY 09/30 765P @.96")
    assert s.action is SignalAction.BUY and s.parser == "alert_card"
    assert (s.symbol, s.strike, s.option_type, s.expiration) == ("SPY", Decimal("765"), OptionType.PUT, date(2026, 9, 30))
    assert s.limit_price == Decimal("0.96") and s.quantity == Decimal(1)


@pytest.mark.parametrize("percent_means_exit", [False, True])
def test_update_is_a_trim_never_an_add(percent_means_exit):
    s = _card("Update", "SPY 09/30 765P @1.11 (+15%)", percent_means_exit=percent_means_exit)
    assert s.action is SignalAction.SELL
    assert s.is_partial_close is True and s.source_action == "TRIMMING"
    assert s.quantity is None                 # sized from the position held
    assert s.pnl_percent == Decimal("15")


def test_close_exits_and_never_buys():
    s = _card("Close", "SPY 09/30 765P @.83")
    assert s.action is SignalAction.SELL
    assert s.position_closed is True and s.source_action == "CLOSING"
    assert s.quantity is None


def test_calls_and_whole_dollar_prices_read_too():
    s = _card("Open", "QQQ 10/02 480C @2")
    assert (s.option_type, s.strike, s.limit_price) == (OptionType.CALL, Decimal("480"), Decimal("2"))


def test_a_loss_update_keeps_its_sign():
    s = _card("Update", "SPY 09/30 765P @.80 (-17%)")
    assert s.action is SignalAction.SELL and s.pnl_percent == Decimal("-17")
