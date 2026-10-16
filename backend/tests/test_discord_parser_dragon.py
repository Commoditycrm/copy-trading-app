"""The Dragon channel's alerts — entries and "$SYM @ price | pct%" trims.

Before this parser, on any channel: "$MSFT @ 21.10 | 24% TRIMMING" was read as a
sell of MSFT SHARES, "$CRWV @ 1.29 | 20%" and "$IBIT @ 1.68 | 10% FREE MONEY"
were ignored, and "$WMT 111 CALL 9/25 @ @ 0.51" lost its price.
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import (
    AssetType,
    OptionType,
    OrderKind,
    ParsedMessage,
    ParseStatus,
    SignalAction,
)

TS = datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)


def _parse(text, channel="dragon"):
    return parse_message(ParsedMessage(content=text, posted_at=TS, channel=channel))


def _sig(text, channel="dragon"):
    r = _parse(text, channel)
    assert r.status is ParseStatus.PARSED, r
    assert r.signal.parser == "dragon_alert"
    return r.signal


# ── entries ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, sym, strike, exp, price", [
    ("$MSFT 550 CALL 11/13 @ 17.00", "MSFT", "550", date(2026, 11, 13), "17.00"),
    ("$IBIT 49 CALL 10/16 @ 1.52", "IBIT", "49", date(2026, 10, 16), "1.52"),
    ("$WMT 111 CALL 9/25 @ @ 0.51", "WMT", "111", date(2026, 9, 25), "0.51"),
])
def test_an_entry_is_a_one_lot_limit_buy(text, sym, strike, exp, price):
    s = _sig(text)
    assert (s.action, s.asset_type, s.symbol) == (SignalAction.BUY, AssetType.OPTION, sym)
    assert (s.strike, s.option_type, s.expiration) == (Decimal(strike), OptionType.CALL, exp)
    assert (s.order_type, s.limit_price, s.quantity) == (OrderKind.LIMIT, Decimal(price), Decimal(1))
    assert s.source_action == "ENTRY"


def test_a_put_entry():
    s = _sig("$SPY 650 PUT 9/19 @ 2.10")
    assert s.option_type is OptionType.PUT


# ── trims ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, sym, price, pct", [
    ("$MSFT @ 21.10 | 24% TRIMMING", "MSFT", "21.10", "24"),
    ("$CRWV @ 1.29 | 20%", "CRWV", "1.29", "20"),
    ("$IBIT @ 1.68 | 10% FREE MONEY", "IBIT", "1.68", "10"),
    # Live, missed: "not a trade alert" / read as a stock sell.
    ("$WMT @ 0.71 | 40% ", "WMT", "0.71", "40"),
    ("$CRWV @ 1.45 | 35% TRIMMING", "CRWV", "1.45", "35"),
    ("$WMT UP 100% AT 1.02", "WMT", "1.02", "100"),
    ("$WMT 40% @ 0.71", "WMT", "0.71", "40"),
    ("$WMT @ @ 0.71 | 40%", "WMT", "0.71", "40"),
])
def test_a_trim_sells_part_of_the_held_option(text, sym, price, pct):
    s = _sig(text)
    assert (s.action, s.asset_type, s.symbol) == (SignalAction.SELL, AssetType.OPTION, sym)
    # Contract and size come from the open position, never the alert.
    assert s.contract_unspecified and s.expiry_unspecified
    assert s.strike is None and s.option_type is None and s.quantity is None
    assert s.order_type is OrderKind.MARKET and s.limit_price == Decimal(price)
    assert s.pnl_percent == Decimal(pct)
    assert s.is_partial_close and not s.flatten and not s.position_closed
    assert s.source_action == "TRIMMING"
    # Several held in the ticker: this channel's own contract is taken.
    assert s.channel_contract_first and s.as_dict()["channel_contract_first"] is True


def test_a_trim_with_no_price_still_trims():
    s = _sig("$WMT UP 100%")
    assert s.action is SignalAction.SELL and s.limit_price is None and s.limit_price_unspecified


def test_down_is_a_loss():
    assert _sig("$WMT DOWN 30% AT 0.40").pnl_percent == Decimal("-30")


def test_matched_by_server_name():
    # _channel_of joins channel name, label and server name.
    assert _sig("$WMT @ 0.71 | 40%", channel="alerts Trades Dragon Server").symbol == "WMT"


def test_out_after_the_percentage_closes_it_all():
    s = _sig("$MSFT @ 25.00 | 47% ALL OUT")
    assert s.flatten and s.position_closed and not s.is_partial_close
    assert s.source_action == "CLOSE"


def test_a_losing_trim_keeps_its_sign():
    assert _sig("$CRWV @ 0.80 | -30%").pnl_percent == Decimal("-30")


def test_several_alerts_in_one_message():
    r = _parse("$MSFT 550 CALL 11/13 @ 17.00\n$CRWV @ 1.29 | 20%")
    assert [s.action for s in r.signals] == [SignalAction.BUY, SignalAction.SELL]


def test_pings_are_not_prices():
    s = _sig("$IBIT 49 CALL 10/16 @here @ 1.52")
    assert s.limit_price == Decimal("1.52")


# ── scope ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", ["dragon", "🐉dragon-alerts", "Dragon Trades"])
def test_matched_by_channel_name(channel):
    assert _sig("$CRWV @ 1.29 | 20%", channel=channel).symbol == "CRWV"


@pytest.mark.parametrize("channel", [None, "", "spy-scalps", "jpm"])
def test_other_channels_read_as_before(channel):
    # Unchanged behaviour off the Dragon channel: the trim is still ignored and
    # the entry is still the compact parser's.
    assert _parse("$CRWV @ 1.29 | 20%", channel).status is ParseStatus.IGNORED
    assert _parse("$MSFT 550 CALL 11/13 @ 17.00", channel).signal.parser == "compact_alert"


def test_chatter_on_the_dragon_channel_is_not_a_trade():
    for text in ("gm everyone", "$MSFT looking strong", "$MSFT @ 21.10", "MSFT 550 CALL 11/13 @ 17",
                 "WMT up 40%"):
        r = _parse(text)
        assert r.status is not ParseStatus.PARSED or r.signal.parser != "dragon_alert", text


def test_a_contract_with_a_pnl_update_is_not_an_entry():
    r = _parse("$MSFT 550 CALL 11/13 @ 17.00 +24%")
    assert not (r.status is ParseStatus.PARSED and r.signal.parser == "dragon_alert"
                and r.signal.action is SignalAction.BUY)


# ── the trader's alerts, as posted ──────────────────────────────────────────

# Fifteen days of the Dragon channel, in order. Every entry must buy its
# contract and every trim must resolve to the contract that entry opened.
_HISTORY = """\
$AAPL 325 CALL 9/2 @ 1.42
$AAPL @ 1.83 | 28%
$AAPL 40%
$NKE 40 CALL 10/16 @ 2.09
$AAPL @ 2.40 | 70%
$AAPL @ 3.40 | 140%
$PBR 21 CALL 9/11 @ 0.23
$PBR @ 0.29 | 26%
$PBR @ 0.32 | 40%
$PBR @ 0.46 | 100%
$PBR @ 0.66 | 200%
$WMT 108 CALL 9/11 @ 0.92
$WMT @ 1.10 | 20%
$WMT @ 1.21 | 31%
$WMT @ 1.40 | 52%
$WMT @ 1.83 | 100%
$IBIT 49 CALL 10/16 @ 1.52
$IBIT @ 1.68 | 10%
$IBM 250 CALL 9/18 @ 1.73
$IBM @ 2.18 | 26%
$IBM @ 4.60 | 165%
$IBIT @ 2.07 | 36%
$WMT 111 CALL 9/25 @ @ 0.51
$WMT @ 0.61 | 20%
$WMT @ 0.65 | 27%
$WMT @ 0.71 | 40%
$WMT UP 100% AT 1.02
$CRWV 96 CALL 10/9 @ 1.07
$CRWV @ 1.29 | 20%
$CRWV @ 1.45 | 35% TRIMMING
$MSFT 550 CALL 11/13 @ 17.00
$MSFT @ 21.10 | 24% TRIMMING"""


def test_fifteen_days_of_alerts():
    posted = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)
    opened = {}
    for line in _HISTORY.splitlines():
        r = parse_message(ParsedMessage(content=line, posted_at=posted, channel="dragon"))
        assert r.status is ParseStatus.PARSED, line
        s = r.signal
        assert s.parser == "dragon_alert" and s.asset_type is AssetType.OPTION, line
        if s.action is SignalAction.BUY:
            assert s.strike and s.option_type and s.expiration and s.limit_price, line
            opened[s.symbol] = (s.strike, s.expiration)
        else:
            assert s.symbol in opened, f"trim before any entry: {line}"
            assert s.is_partial_close and s.pnl_percent > 0, line
    assert opened == {
        "AAPL": (Decimal("325"), date(2026, 9, 2)),
        "NKE": (Decimal("40"), date(2026, 10, 16)),
        "PBR": (Decimal("21"), date(2026, 9, 11)),
        "WMT": (Decimal("111"), date(2026, 9, 25)),
        "IBIT": (Decimal("49"), date(2026, 10, 16)),
        "IBM": (Decimal("250"), date(2026, 9, 18)),
        "CRWV": (Decimal("96"), date(2026, 10, 9)),
        "MSFT": (Decimal("550"), date(2026, 11, 13)),
    }


# ── typed into the Discord popup (the Self channel) ─────────────────────────

def _self(text):
    return parse_message(ParsedMessage(content=text, posted_at=TS, self_channel=True))


@pytest.mark.parametrize("text, sym", [
    # Live: "You hold no position in that contract to close." — read as a sell
    # of MSFT SHARES while the account held MSFT 550C 11/13.
    ("$MSFT @ 21.10 | 24% TRIMMING", "MSFT"),
    ("$WMT @ 0.71 | 40%", "WMT"),
    ("$WMT UP 100% AT 1.02", "WMT"),
])
def test_a_dragon_trim_typed_on_self_trims_the_option(text, sym):
    s = _self(text).signal
    assert s.parser == "dragon_alert"
    assert (s.action, s.asset_type, s.symbol) == (SignalAction.SELL, AssetType.OPTION, sym)
    assert s.contract_unspecified and s.channel_contract_first


@pytest.mark.parametrize("text, parser", [
    # Entries typed on Self are read as they always were.
    ("$MSFT 550 CALL 11/13 @ 17.00", "compact_alert"),
    # Scissors exits and auto-trim's own alerts name a contract: not Dragon's.
    ("✂️ $SPY 769c +361%", "compact_alert"),
    ("✂️ $NVDA 230C 09/28", "compact_alert"),
])
def test_everything_else_on_self_reads_as_before(text, parser):
    r = _self(text)
    assert r.status is ParseStatus.PARSED and r.signal.parser == parser
