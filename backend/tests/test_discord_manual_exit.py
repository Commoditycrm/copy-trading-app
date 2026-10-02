"""Manual exits — the third choice beside "wait for the alert" and auto-trim.

With it on, Kopyya never sells a position on its own: the channel's exit alerts
are recorded and not acted on, auto-trim does not fire, and AI trimming is never
asked. The trader closes it themselves — from Positions, or by typing an exit
into the composer (the Self channel), which still goes through.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.api.discord_sources as ds
import app.api.trades as trades
import app.services.discord_execution as ex
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessageStatus
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType
from app.schemas.discord import DiscordSettingsIn
from app.schemas.order import PlaceOrderIn
from app.services import discord_auto_trim, discord_channel_settings as dcs

FUTURE = datetime.now(timezone.utc).date() + timedelta(days=7)


def _settings(**kw):
    base = dict(
        discord_quantity_multiplier=1, discord_max_per_contract=None, discord_max_per_order=None,
        discord_live_trading=True, discord_trail_percent=Decimal("20"),
        discord_trim_profit_gate_pct=Decimal("20"), discord_trim_stop_pct=Decimal("-25"),
        discord_trim_price_threshold=Decimal("0.90"), discord_trim_trail_amount=Decimal("0.25"),
        discord_auto_trim=False, discord_manual_exit=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── the setting ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("auto, manual, mode", [
    (False, False, "alerts"), (True, False, "auto"), (False, True, "manual"), (True, True, "manual"),
])
def test_exit_mode_reads_the_two_switches(auto, manual, mode):
    assert dcs.exit_mode(_settings(discord_auto_trim=auto, discord_manual_exit=manual)) == mode


@pytest.mark.parametrize("mode, auto, manual", [
    ("alerts", False, False), ("auto", True, False), ("manual", False, True),
])
def test_setting_exit_mode_sets_both_switches(mode, auto, manual):
    ts = _settings(discord_auto_trim=not auto, discord_manual_exit=not manual)
    ds._apply_settings(ts, DiscordSettingsIn(exit_mode=mode), SimpleNamespace(id=uuid.uuid4()))
    assert (ts.discord_auto_trim, ts.discord_manual_exit) == (auto, manual)


def test_turning_auto_trim_on_the_old_way_leaves_manual():
    ts = _settings(discord_manual_exit=True)
    ds._apply_settings(ts, DiscordSettingsIn(auto_trim=True), SimpleNamespace(id=uuid.uuid4()))
    assert (ts.discord_auto_trim, ts.discord_manual_exit) == (True, False)


def test_a_channel_can_have_its_own_exit_mode():
    account = _settings()
    own = dcs.ChannelSettings(account, {})
    own.discord_manual_exit = True
    assert dcs.exits_manual(own) and not dcs.exits_manual(account)
    assert dcs.ChannelSettings(account, own.to_json()).discord_manual_exit is True


# ── auto-trim ────────────────────────────────────────────────────────────────

def test_auto_trim_does_not_fire_under_manual_exits():
    guard = SimpleNamespace(sell_count=0, entry_price=Decimal("1.00"))
    on = _settings(discord_auto_trim=True)
    assert discord_auto_trim.due_rung(on, guard, Decimal("1.50")) == 1
    manual = _settings(discord_auto_trim=True, discord_manual_exit=True)
    assert discord_auto_trim.due_rung(manual, guard, Decimal("1.50")) is None


# ── an exit alert ────────────────────────────────────────────────────────────

class _DB:
    def __init__(self, settings, source=None):
        self._s, self._source = settings, source

    def get(self, model, key):
        return self._source if model is DiscordAlertSource else self._s

    def add(self, obj): pass
    def flush(self): pass


@pytest.fixture
def exit_alert(monkeypatch):
    placed = {}

    def _setup(settings, source=None, guard=True):
        user = SimpleNamespace(id=uuid.uuid4())
        msg = SimpleNamespace(id=uuid.uuid4(), status=DiscordMessageStatus.PARSED, status_reason=None,
                              order_id=None, source_id=uuid.uuid4(),
                              parsed_signal={"action": "SELL", "symbol": "MSFT"})
        g = DiscordPositionGuard(
            user_id=user.id, symbol="MSFT", option_strike=Decimal("100"),
            option_right=OptionRight.CALL.value, option_expiry=FUTURE,
            sell_count=0, entry_price=Decimal("2.00"),
        ) if guard else None
        monkeypatch.setattr(ds.discord_execution, "cancel_stale_entries_for_signal", lambda *a, **k: [])
        monkeypatch.setattr(ds.discord_execution, "resolve", lambda *a, **k: ex.Resolved(
            payload=PlaceOrderIn(
                instrument_type=InstrumentType.OPTION, symbol="MSFT", side=OrderSide.SELL,
                order_type=OrderType.MARKET, quantity=Decimal(4), limit_price=None,
                option_expiry=FUTURE, option_strike=Decimal("100"), option_right=OptionRight.CALL,
            ),
            broker_account_id=uuid.uuid4(), is_closing=True, resolutions={},
            mark_price=Decimal("3.00"), position_entry_price=Decimal("2.00"),
        ))
        monkeypatch.setattr(ds.guards, "find", lambda *a, **k: g)
        created = []
        monkeypatch.setattr(ds.guards, "on_buy", lambda *a, **k: created.append(1) or g)
        monkeypatch.setattr(ds.guards, "retire", lambda *a, **k: None)
        monkeypatch.setattr(ds.events, "publish", lambda *a, **k: None)
        # The opening order is not a channel's here, so the alert's channel decides.
        monkeypatch.setattr(ds.discord_channel_settings, "for_guard", lambda *a, **k: None)
        monkeypatch.setattr(ds.discord_channel_settings, "effective", lambda *a, **k: settings)

        def _place(db_, u, payload, acct_id, bg, req, **kw):
            placed["payload"] = payload
            return SimpleNamespace(id=uuid.uuid4(), status=OrderStatus.SUBMITTED)

        monkeypatch.setattr(trades, "_place_trader_order", _place)
        return _DB(settings, source), user, msg, g, created

    return _setup, placed


def test_an_exit_alert_sells_nothing_under_manual_exits(exit_alert):
    setup, placed = exit_alert
    db, user, msg, guard, created = setup(
        _settings(discord_manual_exit=True), source=SimpleNamespace(channel_id="123"))
    ds._execute_signal(db, user, msg, background=None, request=None)

    assert placed == {}
    assert msg.status is DiscordMessageStatus.PARSED and msg.order_id is None
    assert "manual" in msg.status_reason.lower()
    # The ladder is untouched: no rung spent, no stop set.
    assert (guard.sell_count, guard.stop_price) == (0, None)


def test_manual_exits_do_not_start_a_ladder_on_a_hand_opened_position(exit_alert):
    setup, placed = exit_alert
    db, user, msg, _, created = setup(
        _settings(discord_manual_exit=True), source=SimpleNamespace(channel_id="123"), guard=False)
    ds._execute_signal(db, user, msg, background=None, request=None)
    assert placed == {} and created == []


def test_an_exit_typed_into_the_composer_still_goes_through(exit_alert):
    setup, placed = exit_alert
    db, user, msg, guard, _ = setup(
        _settings(discord_manual_exit=True), source=SimpleNamespace(channel_id="self"))
    ds._execute_signal(db, user, msg, background=None, request=None)
    assert placed["payload"].quantity == Decimal(2)      # rung one: half of 4, up 50%


def test_without_manual_exits_the_alert_trims_as_before(exit_alert):
    setup, placed = exit_alert
    db, user, msg, guard, _ = setup(_settings(), source=SimpleNamespace(channel_id="123"))
    ds._execute_signal(db, user, msg, background=None, request=None)
    assert placed["payload"].quantity == Decimal(2)


# ── "Stopped out of rest of SPY calls" ───────────────────────────────────────

def test_a_stop_out_closes_nothing_under_manual_exits(monkeypatch):
    monkeypatch.setattr(ds, "_channel_exits_manual", lambda *a: True)
    monkeypatch.setattr(ex, "channel_held_contracts",
                        lambda *a, **k: pytest.fail("must not even look the positions up"))
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), order_id=None, status=None,
                          status_reason=None,
                          parsed_signal={"symbol": "SPY", "option_type": "CALL", "close_all_matching": True})
    ds._close_all_from_channel(None, SimpleNamespace(id=uuid.uuid4()), msg, None, None)
    assert msg.status is DiscordMessageStatus.PARSED and msg.order_id is None
    assert "manual" in msg.status_reason.lower() and "SPY calls" in msg.status_reason
