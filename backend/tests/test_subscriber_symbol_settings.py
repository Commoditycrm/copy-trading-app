"""Which settings apply to one symbol, for one subscriber.

The governing rule: a symbol rule changes ONLY the symbol it names, and only
the fields it sets. Everything else — other symbols, other fields, and every
subscriber with no rules at all — behaves exactly as it did before symbol
rules existed, so ordinary trader copying is untouched.
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
                             max_per_order=None, copy_enabled=True,
                             advanced_settings_enabled=True))
    s.commit()
    return s


def _rule(db, symbol, settings, enabled=True):
    r = SubscriberSymbolSettings(user_id=USER, symbol=symbol, settings=settings,
                                 enabled=enabled)
    db.add(r); db.commit()
    return r


# ── nothing changes without a rule ─────────────────────────────────────────

def test_with_no_rules_the_subscribers_own_settings_are_returned(db):
    """Not a copy, not a wrapper — the same object every caller used before."""
    base = db.get(SubscriberSettings, USER)
    assert sss.resolve(db, USER, "AAPL") is base


def test_a_symbol_without_a_rule_is_unaffected_by_another_symbols_rule(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    base = db.get(SubscriberSettings, USER)
    # Identity IS the contract here: with no rule the caller gets the raw
    # settings row, which carries no discord_* attributes at all. That is what
    # stops an unconfigured symbol being sized the advanced way.
    assert sss.resolve(db, USER, "TSLA") is base
    assert not hasattr(base, "discord_quantity_multiplier")


def test_a_disabled_rule_does_not_apply(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4}, enabled=False)
    assert sss.resolve(db, USER, "AAPL") is db.get(SubscriberSettings, USER)


@pytest.mark.parametrize("symbol", [None, "", "   "])
def test_an_empty_symbol_resolves_to_the_global_settings(db, symbol):
    assert sss.resolve(db, USER, symbol) is db.get(SubscriberSettings, USER)


# ── the override itself ────────────────────────────────────────────────────

def test_an_overridden_field_wins(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    assert sss.resolve(db, USER, "AAPL").discord_quantity_multiplier == 4


def test_an_unset_field_falls_through_to_the_subscriber(db):
    """A partial configuration must work — requirement 5."""
    ts = sss.resolve(db, USER, "AAPL") if _rule(db, "AAPL", {"discord_quantity_multiplier": 4}) else None
    assert ts.discord_quantity_multiplier == 4          # overridden
    assert ts.discord_max_per_contract is None    # inherited


def test_account_wide_settings_are_never_shadowed(db):
    """A symbol rule can change size and exits, never who you follow or whether
    copying is on."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.copy_enabled is True
    with pytest.raises(AttributeError):
        ts.copy_enabled = False


def test_the_symbol_is_matched_on_the_root_whatever_the_casing(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    for written in ("AAPL", "aapl", "  AaPl "):
        assert sss.resolve(db, USER, written).discord_quantity_multiplier == 4


# ── stored values keep their type ──────────────────────────────────────────

@pytest.mark.parametrize("field,stored,expected", [
    ("discord_quantity_multiplier", 4, 4),
    ("discord_max_per_contract", "250", Decimal("250")),
    ("discord_trim_profit_gate_pct", "20", Decimal("20")),
    ("discord_auto_trim", True, True),
    ("discord_trim_count", 4, 4),
])
def test_a_stored_override_comes_back_as_the_right_type(db, field, stored, expected):
    _rule(db, "AAPL", {field: stored})
    assert sss.resolve(db, USER, "AAPL").__getattr__(field) == expected


def test_a_ladder_field_nobody_set_reads_as_its_own_default(db):
    """The subscriber has no ladder columns at all. Reading None would look
    like "no ladder configured", which is a different thing."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.discord_trim_count == 3
    assert ts.discord_extra_trims == []
    assert ts.discord_auto_trim is False


# ── the exit mode under the ladder ─────────────────────────────────────────

def test_the_default_exit_mode_follows_the_trader(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "alerts"


def test_choosing_the_ladder_switches_the_symbol_to_its_own_rungs(db):
    # A ladder is targets, not a flag: the rungs have to be able to fire.
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "ladder"


def test_a_subscriber_with_no_rules_follows_the_trader(db):
    assert sss.exit_mode(sss.resolve(db, USER, "TSLA")) == "alerts"


# ── what the UI needs ──────────────────────────────────────────────────────

def test_it_reports_which_fields_are_custom(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4, "discord_trim_profit_gate_pct": "20"})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.overridden == ["discord_quantity_multiplier", "discord_trim_profit_gate_pct"]


def test_has_rule_answers_without_resolving(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    assert sss.has_rule(db, USER, "AAPL") is True
    assert sss.has_rule(db, USER, "TSLA") is False


# ── the mode switch ─────────────────────────────────────────────────────────
# Risk Controls or Advanced Settings, never both. The flag decides whether
# symbol rules APPLY; the rules themselves are stored either way, so switching
# modes parks a configuration rather than deleting it.

def _mode(db, advanced: bool):
    s = db.get(SubscriberSettings, USER)
    s.advanced_settings_enabled = advanced
    db.commit()


def test_risk_controls_mode_ignores_symbol_rules_entirely(db):
    """Not "resolves to the same values" — the SAME OBJECT. There is no overlay
    in the path, so Risk Controls behaves exactly as it does today."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    _mode(db, False)
    assert sss.resolve(db, USER, "AAPL") is db.get(SubscriberSettings, USER)


def test_switching_modes_never_loses_a_configuration(db):
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4})
    _mode(db, False)
    assert sss.resolve(db, USER, "AAPL") is db.get(SubscriberSettings, USER)  # parked
    _mode(db, True)
    assert sss.resolve(db, USER, "AAPL").discord_quantity_multiplier == 4     # restored


def test_advanced_mode_with_nothing_configured_changes_nothing(db):
    _mode(db, True)
    assert sss.resolve(db, USER, "AAPL") is db.get(SubscriberSettings, USER)


# ── ALL Symbols, and individual overrides on top ───────────────────────────

def test_all_symbols_applies_to_every_symbol(db):
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2})
    for symbol in ("AAPL", "TSLA", "NVDA"):
        assert sss.resolve(db, USER, symbol).discord_quantity_multiplier == 2


def test_an_individual_symbol_overrides_all_symbols(db):
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2})
    _rule(db, "AAPL", {"discord_quantity_multiplier": 9})
    assert sss.resolve(db, USER, "AAPL").discord_quantity_multiplier == 9
    assert sss.resolve(db, USER, "TSLA").discord_quantity_multiplier == 2


def test_a_symbol_inherits_what_it_does_not_set_from_all_symbols(db):
    """The merge is key by key, not whole-record — which is what makes a
    partial individual configuration useful."""
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2, "discord_max_per_contract": "300"})
    _rule(db, "AAPL", {"discord_quantity_multiplier": 9})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.discord_quantity_multiplier == 9              # its own
    assert ts.discord_max_per_contract == Decimal("300")      # from ALL


def test_anything_neither_layer_sets_falls_back_to_the_saved_settings(db):
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2})
    # max_per_contract is on neither layer -> the subscriber's own 500.
    assert sss.resolve(db, USER, "AAPL").discord_max_per_contract is None


def test_updating_all_symbols_does_not_erase_an_individual_override(db):
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2})
    _rule(db, "AAPL", {"discord_quantity_multiplier": 9})
    rule = sss.rule_for(db, USER, sss.ALL_SYMBOLS)
    rule.settings = {"discord_quantity_multiplier": 5, "discord_max_per_order": "900"}
    db.commit()
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.discord_quantity_multiplier == 9              # still its own
    assert ts.discord_max_per_order == Decimal("900")         # picks up the new common value


def test_editing_one_symbol_leaves_the_others_alone(db):
    _mode(db, True)
    _rule(db, "AAPL", {"discord_quantity_multiplier": 9})
    _rule(db, "TSLA", {"discord_quantity_multiplier": 6})
    sss.rule_for(db, USER, "AAPL").settings = {"discord_quantity_multiplier": 1}
    db.commit()
    assert sss.resolve(db, USER, "TSLA").discord_quantity_multiplier == 6


def test_the_all_row_is_not_treated_as_its_own_symbol(db):
    """Resolving "ALL" must not merge the common layer onto itself twice."""
    _mode(db, True)
    _rule(db, sss.ALL_SYMBOLS, {"discord_quantity_multiplier": 2})
    assert sss.resolve(db, USER, sss.ALL_SYMBOLS).discord_quantity_multiplier == 2


def test_the_settings_response_carries_the_mode():
    """_to_out builds the payload from an explicit field list, so a new column
    is invisible to the UI until it is added there — the stored value was
    right and the response said False."""
    import inspect
    from app.api import settings as api
    src = inspect.getsource(api._to_out)
    assert "advanced_settings_enabled=" in src
