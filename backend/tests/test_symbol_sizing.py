"""Per-symbol sizing for a copy subscriber.

Three rules, in order of how much damage getting them wrong would do:

  1. A symbol with no rule sizes exactly as it always has.
  2. A rule changes the LIVE setting only. A position already open keeps the
     multiplier it was opened at, so adding a rule mid-position cannot throw
     its adds or exits out of proportion.
  3. A cap fails CLOSED: if anything about the per-symbol lookup goes wrong,
     the subscriber's own cap stands rather than disappearing.
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
                             max_per_order=Decimal("1000"), copy_enabled=True))
    s.commit()
    return s


def _sub(db):
    return db.get(SubscriberSettings, USER)


def _rule(db, symbol, settings, enabled=True):
    db.add(SubscriberSymbolSettings(user_id=USER, symbol=symbol,
                                    settings=settings, enabled=enabled))
    db.commit()


# ── the multiplier ─────────────────────────────────────────────────────────

def test_a_symbol_with_no_rule_uses_the_subscribers_multiplier(db):
    assert ce._symbol_multiplier(db, _sub(db), "AAPL") == Decimal("2.000")


def test_a_rule_changes_the_multiplier_for_that_symbol(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert ce._symbol_multiplier(db, _sub(db), "AAPL") == Decimal("0.5")


def test_other_symbols_are_untouched(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert ce._symbol_multiplier(db, _sub(db), "TSLA") == Decimal("2.000")


def test_a_rule_that_does_not_set_a_multiplier_inherits_it(db):
    _rule(db, "AAPL", {"max_per_contract": "250"})
    assert ce._symbol_multiplier(db, _sub(db), "AAPL") == Decimal("2.000")


@pytest.mark.parametrize("symbol", [None, ""])
def test_no_symbol_falls_back(db, symbol):
    assert ce._symbol_multiplier(db, _sub(db), symbol) == Decimal("2.000")


def test_a_broken_lookup_never_drops_the_trade(db, monkeypatch):
    """Sizing is not a place to propagate an error — refusing to size would
    drop the mirror entirely."""
    import app.services.subscriber_symbol_settings as sss
    monkeypatch.setattr(sss, "resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert ce._symbol_multiplier(db, _sub(db), "AAPL") == Decimal("2.000")


def test_the_instrument_lock_still_wins_over_a_symbol_rule():
    """A position already open keeps what it was opened at. The rule applies to
    the next FRESH entry, never to a holding that is already proportioned."""
    import inspect
    src = inspect.getsource(ce.fanout_order) if hasattr(ce, "fanout_order") else ""
    if not src:
        for name in dir(ce):
            fn = getattr(ce, name)
            if callable(fn) and "locked_mult" in (inspect.getsource(fn) if inspect.isfunction(fn) else ""):
                src = inspect.getsource(fn)
                break
    assert "scale_mult = locked_mult if locked_mult is not None else live_mult" in src


# ── the caps ───────────────────────────────────────────────────────────────

def test_a_symbol_cap_overrides_the_subscribers(db):
    _rule(db, "AAPL", {"max_per_contract": "250", "max_per_order": "400"})
    pc, po = {USER: Decimal("500")}, {USER: Decimal("1000")}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("250") and po[USER] == Decimal("400")


def test_a_cap_the_rule_does_not_mention_is_left_alone(db):
    _rule(db, "AAPL", {"max_per_contract": "250"})
    pc, po = {USER: Decimal("500")}, {USER: Decimal("1000")}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("250") and po[USER] == Decimal("1000")


def test_a_rule_can_lift_a_cap_for_one_symbol(db):
    _rule(db, "AAPL", {"max_per_contract": None})
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert USER not in pc


def test_another_symbols_rule_does_not_touch_this_order(db):
    _rule(db, "TSLA", {"max_per_contract": "1"})
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("500")


def test_the_per_contract_cap_is_ignored_on_a_stock_order(db):
    """Unchanged from the global rule: a per-CONTRACT cap has no meaning on a
    stock mirror."""
    _rule(db, "AAPL", {"max_per_contract": "250"})
    pc, po = {}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", False, pc, po)
    assert pc == {}


def test_a_disabled_rule_leaves_the_caps_alone(db):
    _rule(db, "AAPL", {"max_per_contract": "1"}, enabled=False)
    pc, po = {USER: Decimal("500")}, {}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("500")


def test_a_broken_cap_lookup_leaves_the_subscribers_caps_standing(db, monkeypatch):
    """Fail CLOSED: a cap that vanishes on error is a cap that stops protecting."""
    monkeypatch.setattr(ce, "select", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pc, po = {USER: Decimal("500")}, {USER: Decimal("1000")}
    ce._apply_symbol_caps(db, [USER], "AAPL", True, pc, po)
    assert pc[USER] == Decimal("500") and po[USER] == Decimal("1000")


def test_the_fanout_actually_applies_the_symbol_caps():
    """Testing the helper proves it works; this proves it is reached. The caps
    are overlaid onto the authoritative read, so they bind on the very next
    trade rather than whenever the fanout cache refreshes."""
    import inspect
    src = inspect.getsource(ce)
    # The INVOCATION, not the def — "_apply_symbol_caps(db" matches both.
    assert "_apply_symbol_caps(db, _cap_ids" in src
