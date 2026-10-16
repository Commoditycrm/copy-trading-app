"""Advanced sizing for a copy subscriber.

Advanced settings size an ENTRY the way a Discord channel does: a fixed number
of contracts, or a dollar amount, with at most one cap. That is a real change
in meaning — an advanced entry no longer scales with what the trader traded.

Three rules, in order of how much damage getting them wrong would do:

  1. With Advanced off, or nothing configured, sizing is exactly as before.
  2. Only ENTRIES are sized this way. A close is sized from the position held,
     never from the other side's quantity.
  3. A cap fails CLOSED: if the lookup goes wrong, the subscriber's own cap
     stands rather than disappearing.
"""
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce
from app.models.settings import SubscriberSettings
from app.models.subscriber_symbol_settings import SubscriberSymbolSettings
from app.models.user import User, UserRole
from app.services import subscriber_symbol_settings as sss


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


USER = uuid.uuid4()


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, SubscriberSettings, SubscriberSymbolSettings):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="s@x.com", password_hash="x",
               role=UserRole.SUBSCRIBER, is_active=True))
    s.add(SubscriberSettings(user_id=USER, multiplier=Decimal("2.000"),
                             max_per_contract=Decimal("500"),
                             max_per_order=Decimal("1000"), copy_enabled=True,
                             advanced_settings_enabled=True))
    s.commit()
    return s


def _sub(db):
    return db.get(SubscriberSettings, USER)


def _rule(db, symbol, settings, enabled=True):
    db.add(SubscriberSymbolSettings(user_id=USER, symbol=symbol,
                                    settings=settings, enabled=enabled))
    db.commit()


def _mode(db, advanced):
    _sub(db).advanced_settings_enabled = advanced
    db.commit()


def _entry(symbol="AAPL"):
    return SimpleNamespace(symbol=symbol, is_closing=False, is_partial_close=False,
                           quantity=Decimal(10))


# ── nothing configured changes nothing ─────────────────────────────────────

def test_with_no_rules_sizing_is_unchanged(db):
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) is None


def test_with_advanced_off_a_rule_does_not_size(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    _mode(db, False)
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) is None


def test_another_symbols_rule_does_not_size_this_one(db):
    _rule(db, "TSLA", {"discord_quantity_multiplier": 4})
    assert ce._fixed_entry_size(db, _sub(db), _entry("AAPL"), False) is None


# ── a fixed entry size ─────────────────────────────────────────────────────

def test_an_entry_buys_exactly_the_configured_contracts(db):
    """The trader's 10 is irrelevant — that is what a fixed size means."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) == Decimal(4)


def test_all_symbols_sizes_every_symbol(db):
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 3})
    assert ce._fixed_entry_size(db, _sub(db), _entry("NVDA"), False) == Decimal(3)


def test_a_symbol_overrides_the_common_size(db):
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 3})
    _rule(db, "AAPL", {"discord_quantity_multiplier": 7})
    assert ce._fixed_entry_size(db, _sub(db), _entry("AAPL"), False) == Decimal(7)
    assert ce._fixed_entry_size(db, _sub(db), _entry("TSLA"), False) == Decimal(3)


# ── what must NOT be sized this way ────────────────────────────────────────

@pytest.mark.parametrize("closing_flag,closing_arg", [(True, False), (False, True)])
def test_a_close_is_never_given_a_fixed_size(db, closing_flag, closing_arg):
    """A close sizes from the position held. Giving it the entry size would
    strand part of a holding, or try to sell more than is there."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    order = SimpleNamespace(symbol="AAPL", is_closing=closing_flag,
                            is_partial_close=False, quantity=Decimal(10))
    assert ce._fixed_entry_size(db, _sub(db), order, closing_arg) is None


def test_a_dollar_sized_entry_is_left_to_the_placement_path(db):
    """Dollars needs a live price to become a contract count, which this layer
    does not have — so it falls through rather than guessing."""
    _rule(db, "AAPL", {"discord_size_mode": "dollars", "discord_size_dollars": "2500"})
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) is None


def test_a_broken_lookup_falls_back_to_scaling(db, monkeypatch):
    monkeypatch.setattr(sss, "sizing_for",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) is None


def test_a_zero_contract_count_reads_as_one(db):
    """Inherited from the Discord sizing contract (active_sizing), not decided
    here: 0 is not a tradeable size, so it reads as one contract. Pinned
    because the subscriber path reuses that function and must not drift."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 0})
    assert ce._fixed_entry_size(db, _sub(db), _entry(), False) == Decimal(1)


# ── the caps ───────────────────────────────────────────────────────────────

def test_a_symbol_cap_overrides_the_subscribers(db):
    _rule(db, "AAPL", {"discord_max_per_contract": "250", "discord_max_per_order": "400"})
    pc, po = {USER: Decimal("500")}, {USER: Decimal("1000")}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("250") and po[USER] == Decimal("400")


def test_the_all_layer_caps_every_symbol(db):
    _rule(db, sss.ALL_SYMBOLS, {"discord_max_per_contract": "300"})
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "TSLA", True, pc, po)
    assert pc[USER] == Decimal("300")


def test_a_symbol_cap_beats_the_all_layer(db):
    _rule(db, sss.ALL_SYMBOLS, {"discord_max_per_contract": "300"})
    _rule(db, "AAPL", {"discord_max_per_contract": "900"})
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("900")


def test_a_parked_rule_does_not_change_caps_in_standard_mode(db):
    _rule(db, "AAPL", {"discord_max_per_contract": "1"})
    _mode(db, False)
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("500")


def test_the_per_contract_cap_is_ignored_on_a_stock_order(db):
    _rule(db, "AAPL", {"discord_max_per_contract": "250"})
    pc, po = {}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", False, pc, po)
    assert pc == {}


def test_a_broken_cap_lookup_leaves_the_subscribers_caps_standing(db, monkeypatch):
    """Fail CLOSED: a cap that vanishes on error stopped protecting."""
    monkeypatch.setattr(ce, "select", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pc, po = {USER: Decimal("500")}, {USER: Decimal("1000")}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("500") and po[USER] == Decimal("1000")


def test_the_fanout_applies_both(db):
    import inspect
    src = inspect.getsource(ce)
    assert "_apply_symbol_caps(db, _cap_ids" in src
    assert "_fixed_entry_size(db, sub, trader_order, is_closing_effective)" in src
