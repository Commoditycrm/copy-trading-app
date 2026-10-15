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
                             max_per_order=None, copy_enabled=True))
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
    _rule(db, "AAPL", {"multiplier": "0.5"})
    base = db.get(SubscriberSettings, USER)
    assert sss.resolve(db, USER, "TSLA") is base
    assert sss.resolve(db, USER, "TSLA").multiplier == Decimal("2.000")


def test_a_disabled_rule_does_not_apply(db):
    _rule(db, "AAPL", {"multiplier": "0.5"}, enabled=False)
    assert sss.resolve(db, USER, "AAPL") is db.get(SubscriberSettings, USER)


@pytest.mark.parametrize("symbol", [None, "", "   "])
def test_an_empty_symbol_resolves_to_the_global_settings(db, symbol):
    assert sss.resolve(db, USER, symbol) is db.get(SubscriberSettings, USER)


# ── the override itself ────────────────────────────────────────────────────

def test_an_overridden_field_wins(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert sss.resolve(db, USER, "AAPL").multiplier == Decimal("0.5")


def test_an_unset_field_falls_through_to_the_subscriber(db):
    """A partial configuration must work — requirement 5."""
    ts = sss.resolve(db, USER, "AAPL") if _rule(db, "AAPL", {"multiplier": "0.5"}) else None
    assert ts.multiplier == Decimal("0.5")          # overridden
    assert ts.max_per_contract == Decimal("500")    # inherited


def test_account_wide_settings_are_never_shadowed(db):
    """A symbol rule can change size and exits, never who you follow or whether
    copying is on."""
    _rule(db, "AAPL", {"multiplier": "0.5"})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.copy_enabled is True
    with pytest.raises(AttributeError):
        ts.copy_enabled = False


def test_the_symbol_is_matched_on_the_root_whatever_the_casing(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    for written in ("AAPL", "aapl", "  AaPl "):
        assert sss.resolve(db, USER, written).multiplier == Decimal("0.5")


# ── stored values keep their type ──────────────────────────────────────────

@pytest.mark.parametrize("field,stored,expected", [
    ("multiplier", "0.5", Decimal("0.5")),
    ("max_per_contract", "250", Decimal("250")),
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
    _rule(db, "AAPL", {"multiplier": "0.5"})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.discord_trim_count == 3
    assert ts.discord_extra_trims == []
    assert ts.discord_auto_trim is False


# ── the exit mode under the ladder ─────────────────────────────────────────

def test_the_default_exit_mode_follows_the_trader(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "alerts"


def test_choosing_the_ladder_switches_the_symbol_to_its_own_rungs(db):
    _rule(db, "AAPL", {"discord_auto_trim": True})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "ladder"


def test_a_subscriber_with_no_rules_follows_the_trader(db):
    assert sss.exit_mode(sss.resolve(db, USER, "TSLA")) == "alerts"


# ── what the UI needs ──────────────────────────────────────────────────────

def test_it_reports_which_fields_are_custom(db):
    _rule(db, "AAPL", {"multiplier": "0.5", "discord_trim_profit_gate_pct": "20"})
    ts = sss.resolve(db, USER, "AAPL")
    assert ts.overridden == ["multiplier", "discord_trim_profit_gate_pct"]


def test_has_rule_answers_without_resolving(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert sss.has_rule(db, USER, "AAPL") is True
    assert sss.has_rule(db, USER, "TSLA") is False
