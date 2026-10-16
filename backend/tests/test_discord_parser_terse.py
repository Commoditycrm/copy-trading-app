"""BREAKDOWNSNIPER-style terse alerts, and what execution fills in for them.

Live 2026-09-30 all three were ignored — no trade from the channel at all.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.order import OptionRight
from app.services import discord_execution as ex
from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import OptionType, ParsedMessage, ParseStatus, SignalAction

TS = datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc)


def _sig(text):
    r = parse_message(ParsedMessage(content=text, posted_at=TS))
    assert r.status is ParseStatus.PARSED, r
    return r.signals[0] if getattr(r, "signals", None) else r.signal


# ── parsing ─────────────────────────────────────────────────────────────────

def test_a_glued_contract_is_an_entry_at_the_nearest_expiry():
    s = _sig("AMZN245P @here @Sniper .55 (edited)")
    assert (s.action, s.symbol, s.strike, s.option_type) == (SignalAction.BUY, "AMZN", Decimal("245"), OptionType.PUT)
    assert s.limit_price == Decimal("0.55") and s.quantity == Decimal(1)
    assert s.expiration is None and s.nearest_expiry is True


def test_adding_with_no_contract_adds_to_the_channels_position():
    s = _sig("Adding .4 @here @Sniper")
    assert s.action is SignalAction.BUY and s.symbol is None
    assert s.add_to_latest is True and s.double_up is True
    assert s.limit_price == Decimal("0.4")


def test_add_trim_is_a_trim_of_the_named_ticker():
    s = _sig("Holy moly what an add trim @here @Sniper .63 moving crazy AMZN 25% (edited)")
    assert s.action is SignalAction.SELL and s.symbol == "AMZN"
    assert s.is_partial_close is True and s.strike is None and s.quantity is None
    assert s.limit_price == Decimal("0.63") and s.pnl_percent == Decimal("25")


@pytest.mark.parametrize("text", [
    "good morning @here",
    "trim some AMZN and some TSLA",          # two tickers — which one?
    "AMZN looking strong today",
])
def test_anything_else_is_not_traded(text):
    r = parse_message(ParsedMessage(content=text, posted_at=TS))
    assert r.status is not ParseStatus.PARSED


def test_it_runs_last_so_it_never_takes_another_formats_message():
    from app.services.discord_parsers import PARSERS

    assert PARSERS[-1].name == "terse_alert"


# ── execution: nearest expiry ───────────────────────────────────────────────

_REAL_DATA_EXPIRIES = ex._data_account_expiries


@pytest.fixture(autouse=True)
def _no_data_key(monkeypatch):
    """Broker-path tests below: no Alpaca data key, so nothing goes to the network.
    The data-account tests patch the chain read themselves."""
    monkeypatch.setattr(ex, "_data_account_expiries", lambda *a: None)


def _contract(strike, right, exp):
    return SimpleNamespace(strike_price=strike, type=SimpleNamespace(value=right), expiration_date=exp)


def test_nearest_listed_expiry_is_chosen(monkeypatch):
    monkeypatch.setattr(ex.market_hours, "now_et", lambda: datetime(2026, 9, 30, 9, 30))
    adapter = SimpleNamespace(list_option_contracts=lambda **kw: [
        _contract("245", "put", date(2026, 10, 2)),
        _contract("245", "put", date(2026, 9, 30)),
        _contract("250", "put", date(2026, 9, 30)),
        _contract("245", "call", date(2026, 9, 30)),
    ])
    sig = {"strike": "245", "option_type": "put", "expiration": None}
    res = {}
    out = ex._with_nearest_expiry(adapter, "AMZN", sig, [], res)
    assert out["expiration"] == "2026-09-30" and "nearest" in res["expiration"]


def test_a_held_contract_wins_over_the_chain():
    held = SimpleNamespace(option_strike=Decimal("245"), option_right=OptionRight.PUT,
                           option_expiry=date(2026, 10, 2), quantity=Decimal(5))
    adapter = SimpleNamespace(list_option_contracts=lambda **kw: pytest.fail("chain read"))
    sig = {"strike": "245", "option_type": "put", "expiration": None}
    assert ex._with_nearest_expiry(adapter, "AMZN", sig, [held], {}) is sig


def test_no_listed_contract_is_refused(monkeypatch):
    monkeypatch.setattr(ex.market_hours, "now_et", lambda: datetime(2026, 9, 30, 9, 30))
    adapter = SimpleNamespace(list_option_contracts=lambda **kw: [])
    with pytest.raises(ex.ExecutionRefused):
        ex._with_nearest_expiry(adapter, "AMZN", {"strike": "245", "option_type": "put"}, [], {})


class _FakeChainClient:
    """Stands in for the Alpaca data client; keys by OCC symbol like the real one."""
    def __init__(self, *a, **kw):
        pass

    def get_option_chain(self, req):
        return {
            "AMZN261002P00245000": object(),
            "AMZN260930P00245000": object(),
            "AMZN260930P00250000": object(),   # other strike: ignored
            "AMZN260930C00245000": object(),   # a call: ignored
        }


def _with_data_key(monkeypatch):
    import alpaca.data.historical.option as opt

    from app import config

    monkeypatch.setattr(config.get_settings(), "alpaca_data_api_key", "data-key")
    monkeypatch.setattr(config.get_settings(), "alpaca_data_api_secret", "data-secret")
    monkeypatch.setattr(opt, "OptionHistoricalDataClient", _FakeChainClient)


def test_a_webull_trader_gets_the_nearest_expiry_from_the_data_account(monkeypatch):
    """Webull can't list contracts — the data account's chain answers instead."""
    monkeypatch.setattr(ex, "_data_account_expiries", _REAL_DATA_EXPIRIES)
    monkeypatch.setattr(ex.market_hours, "now_et", lambda: datetime(2026, 9, 30, 9, 30))
    _with_data_key(monkeypatch)
    webull = SimpleNamespace()                     # no list_option_contracts
    res = {}
    out = ex._with_nearest_expiry(webull, "AMZN", {"strike": "245", "option_type": "put"}, [], res)
    assert out["expiration"] == "2026-09-30" and "nearest" in res["expiration"]


def test_the_data_account_is_asked_before_the_broker(monkeypatch):
    monkeypatch.setattr(ex, "_data_account_expiries", _REAL_DATA_EXPIRIES)
    monkeypatch.setattr(ex.market_hours, "now_et", lambda: datetime(2026, 9, 30, 9, 30))
    _with_data_key(monkeypatch)
    alpaca = SimpleNamespace(list_option_contracts=lambda **kw: pytest.fail("broker asked"))
    out = ex._with_nearest_expiry(alpaca, "AMZN", {"strike": "245", "option_type": "put"}, [], {})
    assert out["expiration"] == "2026-09-30"


def test_no_data_key_and_no_broker_listing_is_refused(monkeypatch):
    monkeypatch.setattr(ex.market_hours, "now_et", lambda: datetime(2026, 9, 30, 9, 30))
    with pytest.raises(ex.ExecutionRefused, match="no Alpaca market-data key"):
        ex._with_nearest_expiry(SimpleNamespace(), "AMZN",
                                {"strike": "245", "option_type": "put"}, [], {})


def test_occ_symbols_are_read():
    assert ex._occ_expiry_strike_right("AMZN261002P00245000") == (
        date(2026, 10, 2), Decimal("245"), OptionRight.PUT)
    assert ex._occ_expiry_strike_right("SPY260930C00765500")[1] == Decimal("765.5")
    assert ex._occ_expiry_strike_right("not-a-symbol") is None


# ── execution: JPM Close flattens, adds resolve from the channel ───────────

def test_a_flatten_signal_skips_the_ladder():
    import inspect

    from app.api import discord_sources

    src = inspect.getsource(discord_sources._execute_signal)
    block = src[src.index('if signal.get("flatten"):'):src.index("guards.plan_exit(guard, held")]
    assert "sell_qty=held" in block and "retire=True" in block


def test_add_to_latest_is_resolved_before_the_order_is_built():
    import inspect

    from app.api import discord_sources

    src = inspect.getsource(discord_sources._execute_signal)
    assert src.index("latest_channel_contract(") < src.index("discord_execution.resolve(db, user, signal, sizing)")


# ── "Average down on SPY @0.80" ─────────────────────────────────────────────

def _avg(text):
    return parse_message(ParsedMessage(content=text))


@pytest.mark.parametrize("text,price", [
    ("Average down on SPY @0.80", "0.80"),
    ("averaging down $SPY .8", "0.8"),
    ("avg down on SPY 0.75 @here", "0.75"),
])
def test_average_down_doubles_the_open_position(text, price):
    s = _avg(text).signals[0]
    assert (s.action.value, s.symbol, s.double_up, s.contract_unspecified) == ("BUY", "SPY", True, True)
    assert str(s.limit_price) == price and s.quantity is None


def test_average_down_without_a_price_is_refused():
    r = _avg("Average down on SPY")
    assert r.status is ParseStatus.INVALID and "no price" in r.reason


def test_chatter_about_averaging_down_is_ignored():
    assert _avg("I might average down later").status is ParseStatus.IGNORED


def test_an_at_price_is_never_stripped_as_a_mention():
    s = _avg("AMZN245P @0.55").signals[0]
    assert str(s.limit_price) == "0.55"


def _held(strike, right, qty=3):
    return SimpleNamespace(option_strike=Decimal(strike), option_right=right,
                           option_expiry=date(2026, 10, 1), quantity=Decimal(qty))


def test_execution_doubles_the_one_held_spy_contract():
    sig = _avg("Average down on SPY @0.80").signals[0].as_dict()
    held = [_held("767", OptionRight.CALL, 3)]
    res = {}
    strike, right, expiry = ex._resolve_contract(sig, held, res)
    assert (strike, right) == (Decimal("767"), OptionRight.CALL)
    qty = ex._resolve_quantity(sig, held, strike, right, expiry, False, ex.Sizing(multiplier=4), res)
    assert qty == Decimal(3)          # doubles the 3 held — not your Contracts per alert


def test_execution_refuses_when_two_spy_contracts_are_held():
    sig = _avg("Average down on SPY @0.80").signals[0].as_dict()
    with pytest.raises(ex.ExecutionRefused, match="2 of your open contracts"):
        ex._resolve_contract(sig, [_held("767", OptionRight.CALL), _held("760", OptionRight.PUT)], {})


def test_execution_refuses_when_no_spy_contract_is_held():
    sig = _avg("Average down on SPY @0.80").signals[0].as_dict()
    with pytest.raises(ex.ExecutionRefused, match="no matching position"):
        ex._resolve_contract(sig, [], {})


# ── "In SPY 763P 1.01": a spaced contract after an entry word ───────────────

@pytest.mark.parametrize("text,sym,strike,right,price", [
    ("In SPY 763P @here @Sniper 1.01", "SPY", "763", "PUT", "1.01"),
    ("Entry: QQQ 600P @0.95", "QQQ", "600", "PUT", "0.95"),
    ("In $SPY 765c .80", "SPY", "765", "CALL", "0.80"),
])
def test_an_entry_word_then_a_spaced_contract_is_an_entry(text, sym, strike, right, price):
    s = parse_message(ParsedMessage(content=text)).signals[0]
    assert (s.action.value, s.symbol, str(s.strike), s.option_type.value) == ("BUY", sym, strike, right)
    assert str(s.limit_price) == price and s.nearest_expiry is True


@pytest.mark.parametrize("text", ["SPY 763P hit 1.50", "I'm in SPY 763P 1.01", "watching SPY 763P 1.01"])
def test_a_spaced_contract_without_a_leading_entry_word_is_not_an_entry(text):
    assert parse_message(ParsedMessage(content=text)).status is ParseStatus.IGNORED


def test_an_entry_with_no_price_is_refused():
    assert parse_message(ParsedMessage(content="In SPY 763P")).status is ParseStatus.IGNORED


# ── a pinged, spaced, price-less call (missed live 2026-10-06) ──────────────

def test_a_pinged_spaced_contract_with_no_price_is_an_entry_priced_at_execution():
    s = _sig("QQQ 759P @here @everyone out the gate high risk")
    assert (s.action, s.symbol, s.strike, s.option_type) == (SignalAction.BUY, "QQQ", Decimal("759"), OptionType.PUT)
    assert s.limit_price is None and s.limit_price_unspecified is True
    assert s.nearest_expiry is True and s.quantity == Decimal(1)


def test_a_pinged_glued_contract_with_no_price_is_an_entry_too():
    s = _sig("QQQ759P @here")
    assert s.symbol == "QQQ" and s.limit_price_unspecified is True


@pytest.mark.parametrize("text", [
    "QQQ 759P looking juicy",           # not pinged: commentary
    "SPY 763P hit 1.50",                # not pinged
    "QQQ 759P @here up 40%",            # pinged, but a gain report
    "QQQ 759P @here trimmed",           # pinged, but an exit
    "QQQ 759P @everyone out of the rest",
])
def test_these_are_never_a_buy(text):
    r = parse_message(ParsedMessage(content=text, posted_at=TS))
    assert not any(s.action is SignalAction.BUY for s in (r.signals or []))


def test_execution_buys_a_price_less_entry_at_the_live_ask():
    sig = {"action": "BUY", "limit_price": None, "limit_price_unspecified": True}
    adapter = SimpleNamespace()
    resolutions = {}
    original = ex._quote
    ex._quote = lambda *a, **k: (Decimal("0.40"), Decimal("0.44"))
    try:
        price = ex._resolve_limit_price(sig, adapter, "QQQ", Decimal(759), OptionRight.PUT,
                                        date(2026, 10, 6), ex.OrderSide.BUY, resolutions)
    finally:
        ex._quote = original
    assert price == Decimal("0.44") and "live quote" in resolutions["limit_price"]


# ── a mid-paragraph "Adding .50" ────────────────────────────────────────────
# Live 2026-10-09: the instruction sat inside a paragraph of commentary, so the
# anchored _ADD_RE never saw it and the alert produced no trade at all. What
# separates it from musing is the stated result — "new avg .73" is only written
# after an add that really happened.

REAL_ALERT = (
    "AAPL is being very stupid QQQ breaking Lows and somehow after a basic gap "
    "down on bad news bulls are buying the dip on AAPL sadly @everyone @Sniper  "
    "I do believe we were just early . Adding .50 here new avg .73 if you want "
    "I truly don’t think this PA on AAPL makes sense rn"
)


def test_the_live_alert_adds_to_the_aapl_contract():
    s = _sig(REAL_ALERT)
    assert s.action is SignalAction.BUY
    assert s.limit_price == Decimal("0.50")   # the add price, not the new average
    assert s.double_up is True
    # QQQ is named too, but only as market colour — the add's own sentence
    # names AAPL, which is the position being averaged.
    assert s.symbol == "AAPL"


def test_the_new_average_is_never_mistaken_for_the_add_price():
    assert _sig(REAL_ALERT).limit_price != Decimal("0.73")


def test_a_message_initial_add_still_names_no_symbol():
    # Unchanged behaviour: the contract comes from add_to_latest.
    s = _sig("Adding .50 here new avg .73")
    assert s.symbol is None and s.add_to_latest is True and s.limit_price == Decimal("0.50")


@pytest.mark.parametrize("text,symbol", [
    ("TSLA looks done here. Adding .40 new avg .61", "TSLA"),
    ("Adding .50 here new avg .73, down 30%", None),   # a percent is not an exit
])
def test_a_corroborated_mid_sentence_add_fires(text, symbol):
    s = _sig(text)
    assert s is not None and s.double_up is True and s.symbol == symbol


@pytest.mark.parametrize("text", [
    "I might add .50, new avg would be .73",        # hedged: an average that WOULD be
    "not adding .50 here, new avg stays .73",       # negated
    "I do believe we were early . Adding .50 here",  # no stated average to corroborate
    "I might be adding .50 later if it keeps dropping",
    "leave room to add in case they want a bit more of a bounce",
    "sold half. new avg .73 after adding .50",      # an exit, not an add
])
def test_chatter_about_adding_never_buys(text):
    # These are IGNORED, not PARSED, so _sig's status assert does not apply —
    # check directly that nothing would be bought.
    r = parse_message(ParsedMessage(content=text, posted_at=TS))
    assert not any(sig.action is SignalAction.BUY for sig in (r.signals or [])), \
        f"{text!r} must not place an order"


def test_price_action_is_not_read_as_a_ticker():
    # "this PA on AAPL" previously counted as two tickers, leaving the add
    # unable to name the position it was averaging.
    from app.services.discord_parsers.terse_alert import _tickers_in
    assert _tickers_in("I truly don't think this PA on AAPL makes sense") == ["AAPL"]
