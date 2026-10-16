"""A trader's committed cut gets you out — the sure exit.

With Manual exits, "Auto exit on cut" (default ticked) still sells on a cut; a
partial cut sells exactly its share, rounded up, and is not a ladder rung; under
take-profit orders a partial cut is not ignored as a trim alert. Before selling,
a cut waits for the ladder's cancels to clear and sells what is really held. A
market sell refused as an order type goes once more as a limit under the bid.
A position assigned to Self is always left alone.
"""
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from fastapi import HTTPException

import app.api.discord_sources as ds
import app.api.trades as trades
from app.models.discord_message import DiscordMessageStatus
from app.models.settings import TraderSettings
from app.schemas.discord import DiscordSettingsIn
from app.services import discord_channel_settings as dcs

from test_discord_manual_exit import _settings, exit_alert  # noqa: E402,F401 — fixture

CUT = {"action": "SELL", "symbol": "MSFT", "flatten": True, "source_action": "CUTTING"}
HALF = {"action": "SELL", "symbol": "MSFT", "is_partial_close": True,
        "source_action": "CUTTING_PART", "cut_fraction": "0.5"}


@pytest.fixture(autouse=True)
def _held(monkeypatch):
    """By default the broker still holds the 4 resolve() sized."""
    state = SimpleNamespace(held=Decimal(4), reads=0)

    def _now(db, resolved, p, released):
        state.reads += 1
        return state.held

    monkeypatch.setattr(ds, "_cut_held_now", _now)
    return state


def _run(setup, settings, signal, channel="123", after_setup=None):
    db, user, msg, guard, _ = setup(settings, source=SimpleNamespace(channel_id=channel))
    if after_setup is not None:
        after_setup()
    msg.parsed_signal = dict(signal)
    ds._execute_signal(db, user, msg, background=None, request=None)
    return msg, guard


# ── the setting ─────────────────────────────────────────────────────────────

def test_it_defaults_to_ticked():
    assert dcs.cut_exits(TraderSettings(user_id=uuid.uuid4(), discord_cut_exits=True)) is True
    assert dcs.cut_exits(None) is True


def test_a_channel_whose_own_settings_predate_it_reads_ticked():
    assert dcs.ChannelSettings(_settings(discord_cut_exits=False), {}).discord_cut_exits is True


def test_it_saves_per_channel():
    own = dcs.ChannelSettings(_settings(), {})
    ds._apply_settings(own, DiscordSettingsIn(cut_exits=False), SimpleNamespace(id=uuid.uuid4()))
    assert own.to_json()["discord_cut_exits"] is False
    assert ds._settings_out(TraderSettings(user_id=uuid.uuid4(), discord_cut_exits=False)).cut_exits is False


# ── Manual exits ────────────────────────────────────────────────────────────

def test_a_cut_sells_under_manual_exits_when_ticked(exit_alert):
    setup, placed = exit_alert
    msg, _ = _run(setup, _settings(discord_manual_exit=True, discord_cut_exits=True), CUT)
    assert placed["payload"].quantity == Decimal(4)


def test_a_cut_does_not_sell_under_manual_exits_when_unticked(exit_alert):
    setup, placed = exit_alert
    msg, _ = _run(setup, _settings(discord_manual_exit=True, discord_cut_exits=False), CUT)
    assert placed == {} and "manual" in msg.status_reason.lower()


def test_any_other_exit_alert_still_sells_nothing_under_manual(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(discord_manual_exit=True, discord_cut_exits=True),
         {"action": "SELL", "symbol": "MSFT", "flatten": True, "source_action": "STOPPED_OUT"})
    assert placed == {}


def test_a_position_assigned_to_self_is_left_alone(exit_alert, monkeypatch):
    setup, placed = exit_alert
    monkeypatch.setattr(ds.guards, "is_manual", lambda db, g: True)
    msg, _ = _run(setup, _settings(discord_cut_exits=True), CUT)
    assert placed == {} and "Self" in msg.status_reason


def test_a_stop_out_under_manual_still_does_nothing_but_a_cut_goes_through(monkeypatch):
    seen = []
    monkeypatch.setattr(ds, "_channel_exits_manual", lambda *a: True)
    monkeypatch.setattr(ds, "_is_self_alert", lambda db, m: False)
    monkeypatch.setattr(ds.discord_channel_settings, "effective",
                        lambda *a, **k: _settings(discord_manual_exit=True, discord_cut_exits=True))
    monkeypatch.setattr(ds.discord_execution, "channel_held_contracts",
                        lambda *a, **k: seen.append("looked") or [])
    monkeypatch.setattr(ds.discord_execution, "mark_failed", lambda m, r: setattr(m, "status_reason", r))
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), order_id=None, status=None,
                          status_reason=None,
                          parsed_signal={"symbol": "SPY", "option_type": "CALL",
                                         "close_all_matching": True, "source_action": "CUTTING"})
    ds._close_all_from_channel(None, SimpleNamespace(id=uuid.uuid4()), msg, None, None)
    assert seen == ["looked"], "a cut under Manual + Auto exit on cut must go on to close"


# ── partial cuts ────────────────────────────────────────────────────────────

def test_cut_half_sells_half_rounded_up_and_is_not_a_rung(exit_alert):
    setup, placed = exit_alert
    msg, guard = _run(setup, _settings(), HALF)
    assert placed["payload"].quantity == Decimal(2)              # half of 4
    assert guard.sell_count == 0, "the trader called the size: no rung spent"


def test_a_third_of_four_is_two(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(), {**HALF, "cut_fraction": str(Decimal(1) / 3)})
    assert placed["payload"].quantity == Decimal(2)              # 1.33 rounds up


def test_a_partial_cut_is_not_ignored_under_take_profit_orders(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(discord_tp_orders=True), HALF)
    assert placed["payload"].quantity == Decimal(2)


def test_a_partial_cut_sells_under_manual_when_ticked(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(discord_manual_exit=True, discord_cut_exits=True), HALF)
    assert placed["payload"].quantity == Decimal(2)


# ── the sure exit ───────────────────────────────────────────────────────────

def test_a_cut_sells_what_is_really_held(exit_alert, _held):
    setup, placed = exit_alert
    _held.held = Decimal(3)                                      # a stop took 1 meanwhile
    _run(setup, _settings(), CUT)
    assert placed["payload"].quantity == Decimal(3) and _held.reads == 1


def test_nothing_held_means_nothing_sold(exit_alert, _held):
    setup, placed = exit_alert
    _held.held = Decimal(0)
    msg, _ = _run(setup, _settings(), CUT)
    assert placed == {} and "nothing left" in msg.status_reason


def test_a_read_that_fails_keeps_the_worked_out_size(exit_alert, _held):
    setup, placed = exit_alert
    _held.held = None
    _run(setup, _settings(), CUT)
    assert placed["payload"].quantity == Decimal(4)


def test_an_ordinary_exit_alert_does_not_re_read(exit_alert, _held):
    setup, placed = exit_alert
    _run(setup, _settings(), {"action": "SELL", "symbol": "MSFT", "flatten": True,
                              "source_action": "CLOSING"})
    assert _held.reads == 0


def test_a_market_sell_refused_as_a_type_goes_again_under_the_bid(exit_alert, monkeypatch):
    setup, placed = exit_alert
    calls = []

    def _place(db_, u, payload, acct_id, bg, req, **kw):
        calls.append(payload)
        if payload.order_type.value == "market":
            raise HTTPException(502, "broker_error: This contract does not support market orders")
        return SimpleNamespace(id=uuid.uuid4(), status="submitted")

    monkeypatch.setattr(ds, "_below_the_bid", lambda p: Decimal("0.45"))
    msg, _ = _run(setup, _settings(), CUT,
                  after_setup=lambda: monkeypatch.setattr(trades, "_place_trader_order", _place))
    assert [c.order_type.value for c in calls] == ["market", "limit"]
    assert calls[1].limit_price == Decimal("0.45") and calls[1].quantity == Decimal(4)


def test_any_other_refusal_is_not_retried(exit_alert, monkeypatch):
    setup, placed = exit_alert
    calls = []

    def _place(db_, u, payload, acct_id, bg, req, **kw):
        calls.append(payload)
        raise HTTPException(502, "broker_error: insufficient position")

    monkeypatch.setattr(ds.discord_execution, "mark_failed", lambda m, r: setattr(m, "status_reason", r))
    msg, _ = _run(setup, _settings(), CUT,
                  after_setup=lambda: monkeypatch.setattr(trades, "_place_trader_order", _place))
    assert len(calls) == 1 and "rejected" in msg.status_reason.lower()


def test_below_the_bid_is_ten_percent_under_and_never_below_a_cent(monkeypatch):
    from app.services import market_data_stream
    p = SimpleNamespace(symbol="QQQ", option_expiry=None, option_strike=Decimal("720"), option_right="put")
    monkeypatch.setattr("app.brokers.alpaca.build_occ_symbol", lambda *a: "QQQ")
    monkeypatch.setattr(market_data_stream, "data_option_bid_ask", lambda occ: (Decimal("0.50"), Decimal("0.52")))
    assert ds._below_the_bid(p) == Decimal("0.45")
    monkeypatch.setattr(market_data_stream, "data_option_bid_ask", lambda occ: (Decimal("0.01"), Decimal("0.02")))
    assert ds._below_the_bid(p) == Decimal("0.01")
    monkeypatch.setattr(market_data_stream, "data_option_bid_ask", lambda occ: (None, None))
    assert ds._below_the_bid(p) is None


# ── Take-profit orders ──────────────────────────────────────────────────────

def test_a_cut_sells_under_take_profit_orders_when_ticked(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(discord_tp_orders=True, discord_cut_exits=True), CUT)
    assert placed["payload"].quantity == Decimal(4)


def test_a_cut_is_left_to_the_take_profits_when_unticked(exit_alert):
    setup, placed = exit_alert
    msg, guard = _run(setup, _settings(discord_tp_orders=True, discord_cut_exits=False), CUT)
    assert placed == {} and "take-profit orders" in msg.status_reason
    assert (guard.sell_count, guard.stop_price) == (0, None)


def test_a_partial_cut_is_left_to_the_take_profits_when_unticked(exit_alert):
    setup, placed = exit_alert
    _run(setup, _settings(discord_tp_orders=True, discord_cut_exits=False), HALF)
    assert placed == {}


def test_a_full_close_that_is_not_a_cut_still_closes_under_take_profit_orders(exit_alert):
    """Unticking it is about cuts only: "Out" / "Stopped out" close as before."""
    setup, placed = exit_alert
    _run(setup, _settings(discord_tp_orders=True, discord_cut_exits=False),
         {"action": "SELL", "symbol": "MSFT", "flatten": True, "source_action": "CLOSING"})
    assert placed["payload"].quantity == Decimal(4)


def test_a_cut_close_all_is_left_to_the_take_profits_when_unticked(monkeypatch):
    seen = []
    monkeypatch.setattr(ds, "_channel_exits_manual", lambda *a: False)
    monkeypatch.setattr(ds, "_is_self_alert", lambda db, m: False)
    monkeypatch.setattr(ds.discord_channel_settings, "effective",
                        lambda *a, **k: _settings(discord_tp_orders=True, discord_cut_exits=False))
    monkeypatch.setattr(ds.discord_execution, "channel_held_contracts",
                        lambda *a, **k: seen.append("looked") or [])
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), order_id=None, status=None,
                          status_reason=None,
                          parsed_signal={"symbol": "SPY", "option_type": "CALL",
                                         "close_all_matching": True, "source_action": "CUTTING"})
    ds._close_all_from_channel(None, SimpleNamespace(id=uuid.uuid4()), msg, None, None)
    assert seen == [] and "take-profit orders" in msg.status_reason
