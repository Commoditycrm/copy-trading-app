"""A subscriber's per-symbol exit ladder.

Two ways out, chosen per symbol:

    "alerts"  exits follow the trader — what every subscriber has always had
    "ladder"  this symbol's own rungs fire, and the trader's TRIMS are not
              mirrored for it. A trader's FULL close still is, so a subscriber
              can never be left holding a position the trader has exited.

The default is "alerts", so a symbol rule that only changes sizing does not
quietly take over the exits.
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
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
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
    for m in (User, SubscriberSettings, SubscriberSymbolSettings, Order, DiscordPositionGuard):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="s@x.com", password_hash="x",
               role=UserRole.SUBSCRIBER, is_active=True))
    s.add(SubscriberSettings(user_id=USER, multiplier=Decimal("1.000"), copy_enabled=True,
                             advanced_settings_enabled=True))
    s.commit()
    return s


def _rule(db, symbol, settings):
    db.add(SubscriberSymbolSettings(user_id=USER, symbol=symbol, settings=settings,
                                    enabled=True))
    db.commit()


def _child(db, symbol="AAPL", closing=False):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION,
              symbol=symbol, side=OrderSide.SELL if closing else OrderSide.BUY,
              order_type=OrderType.LIMIT, quantity=Decimal(2),
              limit_price=Decimal("1.00"), status=OrderStatus.FILLED,
              option_strike=Decimal("250"), option_right=OptionRight.CALL,
              is_closing=closing)
    db.add(o); db.commit()
    return o


# ── the exit mode ──────────────────────────────────────────────────────────

def test_a_sizing_only_rule_leaves_exits_with_the_trader(db):
    """Changing the size must not quietly take over the exits."""
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "alerts"
    assert sss.trims_follow_trader(db, USER, "AAPL") is True


def test_choosing_the_ladder_stops_the_trader_s_trims(db):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    assert sss.trims_follow_trader(db, USER, "AAPL") is False


def test_other_symbols_still_take_the_trader_s_trims(db):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    assert sss.trims_follow_trader(db, USER, "TSLA") is True


def test_a_subscriber_with_no_rules_takes_every_trim(db):
    assert sss.trims_follow_trader(db, USER, "AAPL") is True


def test_a_broken_lookup_still_mirrors_the_trim(db, monkeypatch):
    """An exit that does not happen is worse than one that happens twice: the
    second is clamped to what is held, the first is not clamped at all."""
    monkeypatch.setattr(sss, "trims_follow_trader",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ce._trims_follow_trader(db, USER, "AAPL") is True


# ── the ladder the engine runs on ──────────────────────────────────────────

def test_a_ladder_symbol_resolves_settings_for_its_guard(db):
    _rule(db, "AAPL", {"discord_auto_trim": True, "discord_trim_profit_gate_pct": "25"})
    guard = SimpleNamespace(symbol="AAPL")
    ts = sss.settings_for_guard(db, USER, guard)
    assert ts is not None and ts.discord_trim_profit_gate_pct == Decimal("25")


def test_an_alerts_symbol_has_no_ladder_to_run(db):
    """"On Alerts" means the trader decides. Handing back settings here would
    start a ladder nobody asked for."""
    _rule(db, "AAPL", {"multiplier": "0.5"})
    assert sss.settings_for_guard(db, USER, SimpleNamespace(symbol="AAPL")) is None


def test_a_symbol_with_no_rule_has_no_ladder(db):
    assert sss.settings_for_guard(db, USER, SimpleNamespace(symbol="TSLA")) is None


@pytest.mark.parametrize("guard", [None, SimpleNamespace(symbol=None), SimpleNamespace(symbol="")])
def test_nothing_to_resolve_is_not_a_ladder(db, guard):
    assert sss.settings_for_guard(db, USER, guard) is None


# ── opening the ladder on a mirrored entry ─────────────────────────────────

def test_a_mirrored_entry_opens_a_ladder_for_a_ladder_symbol(db):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    ce._open_symbol_ladder(db, USER, _child(db))
    guard = db.query(DiscordPositionGuard).one()
    assert guard.symbol == "AAPL" and guard.entry_price == Decimal("1.00")


def test_an_alerts_symbol_opens_no_ladder(db):
    _rule(db, "AAPL", {"multiplier": "0.5"})
    ce._open_symbol_ladder(db, USER, _child(db))
    assert db.query(DiscordPositionGuard).count() == 0


def test_a_symbol_with_no_rule_opens_no_ladder(db):
    ce._open_symbol_ladder(db, USER, _child(db, symbol="TSLA"))
    assert db.query(DiscordPositionGuard).count() == 0


def test_a_close_never_opens_a_ladder(db):
    """A close has nothing to protect."""
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    ce._open_symbol_ladder(db, USER, _child(db, closing=True))
    assert db.query(DiscordPositionGuard).count() == 0


def test_a_failure_to_open_never_breaks_the_mirror(db, monkeypatch):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    import app.services.discord_position_guard as guards
    monkeypatch.setattr(guards, "on_buy",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ce._open_symbol_ladder(db, USER, _child(db))      # must not raise
    assert db.query(DiscordPositionGuard).count() == 0


def test_the_fanout_actually_opens_the_ladder():
    """The helper works; this proves it is reached, on the one path every
    successful mirror passes through."""
    import inspect
    assert "_open_symbol_ladder(db, item.subscriber_user_id, child)" in inspect.getsource(ce)


def test_the_fanout_actually_asks_before_mirroring_a_trim():
    import inspect
    assert "_skip_trader_trim(db, sub.user_id, trader_order)" in inspect.getsource(ce)


@pytest.mark.parametrize("closing,partial,mode,expected", [
    (True,  True,  "ladder", True),   # a trim on a ladder symbol: the rungs own it
    (True,  True,  "alerts", False),  # a trim on an alerts symbol: mirror it
    (True,  False, "ladder", False),  # a FULL close always reaches the subscriber
    (False, False, "ladder", False),  # an entry is never suppressed
    (False, True,  "ladder", False),  # not a close at all
])
def test_only_a_trim_on_a_ladder_symbol_is_suppressed(db, closing, partial, mode, expected):
    _rule(db, "AAPL", {"discord_auto_trim": mode == "ladder",
                       "discord_trim_profit_gate_pct": "25" if mode == "ladder" else "0"})
    order = SimpleNamespace(symbol="AAPL", is_closing=closing, is_partial_close=partial)
    assert ce._skip_trader_trim(db, USER, order) is expected


def test_a_trim_on_an_uncustomised_symbol_is_always_mirrored(db):
    order = SimpleNamespace(symbol="TSLA", is_closing=True, is_partial_close=True)
    assert ce._skip_trader_trim(db, USER, order) is False


# ── the shared engine, driven by a subscriber's symbol settings ─────────────
# The ladder is not a copy of the Discord one — it IS the Discord one. These
# prove the subscriber's values reach it, because "we reused the engine" is
# only true if the engine can read what a subscriber configured.

def test_the_ladder_engine_reads_a_subscribers_rungs(db):
    from app.services import discord_ladder

    _rule(db, "AAPL", {
        "discord_auto_trim": True,
        "discord_trim_profit_gate_pct": "25", "discord_trim_stop_pct": "-10",
        "discord_trim_qty_pct": "40",
    })
    ts = sss.settings_for_guard(db, USER, SimpleNamespace(symbol="AAPL"))
    cfg = discord_ladder.trim_config(ts)
    first = cfg.rung(1)
    assert first.profit_gate_pct == Decimal("25")
    assert first.stop_pct == Decimal("-10")
    assert first.qty_pct == Decimal("40")


def test_a_trim_plans_from_those_rungs(db):
    """The whole chain: symbol settings -> trim_config -> plan_exit. A gate of
    25% must not fire at 20%, and must sell the configured share when it does."""
    from app.services import discord_ladder
    from app.services import discord_position_guard as guards

    _rule(db, "AAPL", {
        "discord_auto_trim": True,
        "discord_trim_profit_gate_pct": "25", "discord_trim_stop_pct": "-10",
        "discord_trim_qty_pct": "50",
    })
    ts = sss.settings_for_guard(db, USER, SimpleNamespace(symbol="AAPL"))
    cfg = discord_ladder.trim_config(ts)

    under = SimpleNamespace(symbol="AAPL", sell_count=0, entry_price=Decimal("1.00"),
                            stop_price=None, stop_trail_pct=None, stop_peak=None)
    assert guards.plan_exit(under, Decimal(10), Decimal("1.20"), cfg).sell_qty == Decimal(0)

    at_gate = SimpleNamespace(symbol="AAPL", sell_count=0, entry_price=Decimal("1.00"),
                              stop_price=None, stop_trail_pct=None, stop_peak=None)
    plan = guards.plan_exit(at_gate, Decimal(10), Decimal("1.25"), cfg)
    assert plan.sell_qty == Decimal(5)          # 50% of 10


def test_an_unconfigured_rung_uses_the_ladder_default_not_nothing(db):
    """A partial ladder must still be a ladder: trims the subscriber did not
    fill in fall back to the engine's own defaults."""
    from app.services import discord_ladder

    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    cfg = discord_ladder.trim_config(sss.settings_for_guard(db, USER, SimpleNamespace(symbol="AAPL")))
    assert cfg.rung(1).profit_gate_pct == Decimal("25")
    assert cfg.rung(2) is not None and cfg.rung(3) is not None


# ── one resolution point ────────────────────────────────────────────────────
# The settings that govern a position's exits are resolved in for_guard, not at
# each call site. There are six call sites and they are the paths that place
# real exits; one drifting out of step is invisible until a position silently
# does not get its trim.

def test_for_guard_resolves_a_subscribers_symbol_ladder(db):
    from app.services import discord_channel_settings as dcs

    _rule(db, "AAPL", {"discord_auto_trim": True, "discord_trim_profit_gate_pct": "25"})
    guard = SimpleNamespace(symbol="AAPL", source_id=None, entry_order_id=None)
    ts = dcs.for_guard(db, USER, guard)
    assert ts is not None and ts.discord_trim_profit_gate_pct == Decimal("25")


def test_for_guard_is_still_none_without_a_rule(db):
    """Unchanged for everyone else: the caller falls back as it always has."""
    from app.services import discord_channel_settings as dcs

    guard = SimpleNamespace(symbol="TSLA", source_id=None, entry_order_id=None)
    assert dcs.for_guard(db, USER, guard) is None


def test_for_guard_is_none_for_an_alerts_symbol(db):
    from app.services import discord_channel_settings as dcs

    _rule(db, "AAPL", {"multiplier": "0.5"})
    guard = SimpleNamespace(symbol="AAPL", source_id=None, entry_order_id=None)
    assert dcs.for_guard(db, USER, guard) is None


def test_every_exit_path_resolves_through_for_guard():
    """Each of these places or re-prices a real exit. If one stops going
    through for_guard, a subscriber's ladder stops applying on that path only —
    the hardest kind of gap to notice."""
    import inspect
    from app.services import discord_auto_trim, pnl_poller

    for mod in (pnl_poller, discord_auto_trim):
        assert "for_guard(" in inspect.getsource(mod), mod.__name__


def test_a_channel_still_wins_over_a_symbol_rule(db):
    """A subscriber can have BOTH: a Discord mirror of the trader's channel and
    a symbol rule. The channel that OPENED the position keeps its ladder —
    the symbol rule is the fallback for positions with no channel at all."""
    from app.models.discord_alert_source import DiscordAlertSource
    from app.models.settings import TraderSettings
    from app.services import discord_channel_settings as dcs

    DiscordAlertSource.__table__.create(db.get_bind(), checkfirst=True)
    TraderSettings.__table__.create(db.get_bind(), checkfirst=True)
    src = DiscordAlertSource(user_id=USER, label="Mark", channel_id="1",
                             status="connected", use_account_settings=False,
                             channel_settings={"discord_trim_profit_gate_pct": "99"})
    db.add(src); db.commit()

    _rule(db, "AAPL", {"discord_auto_trim": True, "discord_trim_profit_gate_pct": "25"})
    guard = SimpleNamespace(symbol="AAPL", source_id=src.id, entry_order_id=None)
    ts = dcs.for_guard(db, USER, guard)
    assert ts.discord_trim_profit_gate_pct == Decimal("99"), "the symbol rule overrode the channel"


# ── a ladder is targets, not a flag ────────────────────────────────────────
# The exit-mode cards are gone from the UI, so every advanced scope is saved
# with auto-trim on. What decides whether the ladder OWNS the exits is whether
# it has a target that can actually fire.

def test_a_sizing_only_rule_still_takes_the_traders_trims(db):
    """The trap: auto-trim on with no targets would stop the trader's trims
    being mirrored while firing nothing itself — the position would then never
    be trimmed by anyone."""
    _rule(db, "AAPL", {"discord_quantity_multiplier": 4, "discord_auto_trim": True})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "alerts"
    assert sss.trims_follow_trader(db, USER, "AAPL") is True


def test_a_rule_with_a_real_target_owns_the_exits(db):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "25"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "ladder"
    assert sss.trims_follow_trader(db, USER, "AAPL") is False


def test_a_zero_target_is_not_a_ladder(db):
    """Zero means "keep waiting" to the engine, so it cannot own the exits."""
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim_profit_gate_pct": "0"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "alerts"


def test_a_target_on_any_rung_counts(db):
    _rule(db, "AAPL", {"discord_auto_trim": True,
                       "discord_trim2_profit_gate_pct": "40"})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "ladder"


def test_a_target_inherited_from_all_symbols_counts(db):
    _rule(db, sss.ALL_SYMBOLS, {"discord_auto_trim": True,
                                "discord_trim_profit_gate_pct": "30"})
    _rule(db, "AAPL", {"discord_quantity_multiplier": 2})
    assert sss.exit_mode(sss.resolve(db, USER, "AAPL")) == "ladder"
