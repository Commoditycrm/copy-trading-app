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
