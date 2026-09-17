"""Hand-pinned prices for testing the ladder without market movement.

The safety properties matter more than the feature here: this thing feeds the
real enforcement path, so the tests that count are the ones proving it stays off
unless asked, stays inside one trader, and expires on its own.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import price_override as po

EXP = date(2026, 9, 18)


@pytest.fixture
def on(monkeypatch):
    """Switch the feature on and start from a clean store."""
    monkeypatch.setattr(po, "enabled", lambda: True)
    monkeypatch.setattr(po, "_redis", lambda: None)      # memory path
    po._MEM.clear()
    yield
    po._MEM.clear()


# ── the gate ─────────────────────────────────────────────────────────────────

def test_it_is_off_unless_the_environment_turns_it_on():
    """Default-off is the whole safety story — a pinned price can place real
    orders, so an environment that never asked for this must never get it.

    Asserts the SETTING's default rather than the running config, which a local
    .env is free to switch on."""
    from app.config import Settings
    assert Settings.model_fields["discord_price_override_enabled"].default is False


def test_pinning_is_refused_while_disabled(monkeypatch):
    monkeypatch.setattr(po, "enabled", lambda: False)
    with pytest.raises(RuntimeError):
        po.set_pin(uuid.uuid4(), "MSFT|500|call|2026-09-18", "1.00")


def test_a_pin_is_invisible_while_disabled(monkeypatch, on):
    u = uuid.uuid4()
    key = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(u, key, "1.23")
    monkeypatch.setattr(po, "enabled", lambda: False)
    assert po.get_pin(u, key) is None        # stored, but never honoured


# ── keys ─────────────────────────────────────────────────────────────────────

def test_the_same_contract_always_makes_the_same_key():
    a = po.contract_key("MSFT", 500, "call", EXP)
    b = po.contract_key("msft", Decimal("500.0000"), "call", EXP)
    assert a == b == "MSFT|500|call|2026-09-18"


def test_a_fractional_strike_keeps_its_fraction():
    assert po.contract_key("SPY", Decimal("762.50"), "put", EXP) == "SPY|762.5|put|2026-09-18"


def test_calls_and_puts_do_not_share_a_key():
    assert po.contract_key("MSFT", 500, "call", EXP) != po.contract_key("MSFT", 500, "put", EXP)


def test_a_stock_has_a_key_too():
    assert po.contract_key("AAPL") == "AAPL|||"


# ── setting and clearing ─────────────────────────────────────────────────────

def test_a_pin_round_trips(on):
    u = uuid.uuid4()
    k = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(u, k, "1.23")
    assert po.get_pin(u, k) == Decimal("1.23")


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_an_unusable_price_is_refused(on, bad):
    with pytest.raises(ValueError):
        po.set_pin(uuid.uuid4(), "MSFT|500|call|2026-09-18", bad)


def test_clearing_hands_the_position_back(on):
    u = uuid.uuid4()
    k = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(u, k, "1.23")
    po.clear_pin(u, k)
    assert po.get_pin(u, k) is None


def test_clear_all_drops_every_pin_this_trader_has(on):
    u = uuid.uuid4()
    for strike in (500, 510, 520):
        po.set_pin(u, po.contract_key("MSFT", strike, "call", EXP), "1.00")
    assert po.clear_all(u) == 3
    assert po.get_pin(u, po.contract_key("MSFT", 510, "call", EXP)) is None


def test_clear_all_leaves_other_traders_alone(on):
    a, b = uuid.uuid4(), uuid.uuid4()
    k = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(a, k, "1.00")
    po.set_pin(b, k, "2.00")
    po.clear_all(a)
    assert po.get_pin(b, k) == Decimal("2.00")


# ── isolation and expiry ─────────────────────────────────────────────────────

def test_one_traders_pin_is_invisible_to_another(on):
    a, b = uuid.uuid4(), uuid.uuid4()
    k = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(a, k, "1.23")
    assert po.get_pin(b, k) is None


def test_a_pin_expires_on_its_own(on, monkeypatch):
    """A forgotten pin has to stop mattering, or it quietly distorts a position
    long after whoever set it stopped watching."""
    u = uuid.uuid4()
    k = po.contract_key("MSFT", 500, "call", EXP)
    po.set_pin(u, k, "1.23")

    real = po.time.time
    monkeypatch.setattr(po.time, "time", lambda: real() + po._TTL_SECONDS + 1)
    assert po.get_pin(u, k) is None


# ── what the enforcer actually reads ─────────────────────────────────────────

def test_apply_to_matches_a_position_by_its_contract(on):
    u = uuid.uuid4()

    class _Pos:
        symbol = "MSFT"
        option_strike = Decimal("500")
        option_right = "call"
        option_expiry = EXP

    po.set_pin(u, po.contract_key("MSFT", 500, "call", EXP), "1.23")
    assert po.apply_to(u, _Pos()) == Decimal("1.23")


def test_apply_to_returns_nothing_for_an_unpinned_position(on):
    class _Pos:
        symbol = "TSLA"
        option_strike = Decimal("400")
        option_right = "call"
        option_expiry = EXP

    assert po.apply_to(uuid.uuid4(), _Pos()) is None
