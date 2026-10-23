"""Reapply exit ladder: start a position's ladder over from what is held now.

The trader's request (2026-10-22): a button on the position summary's Placed
line that — even after the position was worked on by hand — removes every stop
and open sell, and reapplies the exit rules from the beginning, on the
position as it is.
"""
import uuid
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.api import positions as api
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight
from app.models.user import User, UserRole
from app.services import discord_ladder, discord_position_guard as guards

EXP = date(2026, 10, 23)


def _ts():
    return SimpleNamespace(discord_fill_stop_pct=D("-35"))


@pytest.fixture
def fill_stop(monkeypatch):
    monkeypatch.setattr(discord_ladder, "fill_stop_pct", lambda ts: D("-35"))
    monkeypatch.setattr(discord_ladder, "fill_stop_trails", lambda ts: False)


def test_reapply_starts_the_ladder_over_from_the_current_cost(fill_stop):
    g = SimpleNamespace(
        symbol="QQQ", closed_at=None, sell_count=1, tp_off=True, tp_backoff_until=object(),
        tp_order_id=uuid.uuid4(), tp_stop_order_id=uuid.uuid4(), tp_rung=2, tp_qty=D(3),
        stop_order_id=uuid.uuid4(), trail_qty=D(2), trail_amount=D("0.1"), peak_price=D("1"),
        trail_percent=D("20"), stop_trail_pct=D("15"), stop_peak=D("1.2"),
        entry_price=D("1.03"), stop_price=None, fill_stop_done=False,
    )
    guards.reapply(None, g, _ts(), average_cost=D("1.0300"))
    assert (g.sell_count, g.tp_off, g.tp_backoff_until) == (0, False, None)
    assert (g.tp_order_id, g.tp_stop_order_id, g.tp_rung, g.tp_qty, g.stop_order_id) == (None,) * 5
    assert (g.trail_qty, g.trail_amount, g.peak_price, g.trail_percent) == (None,) * 4
    assert (g.stop_trail_pct, g.stop_peak) == (None, None)
    assert g.entry_price == D("1.03")
    assert g.stop_price == D("0.66"), "the On Fill stop again: -35% of 1.03 (0.6695, to the tick)"
    assert g.fill_stop_done is True


def test_without_an_on_fill_stop_no_stop_is_set(monkeypatch):
    monkeypatch.setattr(discord_ladder, "fill_stop_pct", lambda ts: None)
    g = SimpleNamespace(symbol="QQQ", closed_at=None, sell_count=2, stop_price=D("0.9"),
                        entry_price=D("1"), fill_stop_done=False)
    guards.reapply(None, g, _ts(), average_cost=D("1.10"))
    assert g.stop_price is None and g.sell_count == 0 and g.entry_price == D("1.10")


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch, fill_stop):
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    db.flush()
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x", connection_status="connected")
    src = DiscordAlertSource(user_id=user.id, label="Clint", channel_id=f"c{uuid.uuid4().hex[:6]}",
                             status="connected")
    db.add_all([acct, src])
    db.flush()
    guard = DiscordPositionGuard(user_id=user.id, symbol="QQQ", option_strike=D("716"), option_right="call",
                                 option_expiry=EXP, sell_count=1, entry_price=D("0.55"), stop_price=None,
                                 tp_off=True, source_id=src.id)
    db.add(guard)
    db.commit()
    pos = SimpleNamespace(symbol="QQQ", option_strike=D("716"), option_right=OptionRight.CALL, option_expiry=EXP,
                          quantity=D(9), instrument_type=InstrumentType.OPTION, broker_symbol="QQQ261023C00716000",
                          avg_entry_price=D("1.03"))
    calls = []
    monkeypatch.setattr(api, "adapter_for", lambda a, c: SimpleNamespace(get_positions=lambda: [pos]))
    monkeypatch.setattr(api, "decrypt_json", lambda v: {})
    monkeypatch.setattr("app.services.discord_stop_orders.release_for_position",
                        lambda db, u, p: calls.append("ladder released") or True)
    monkeypatch.setattr(api, "_cancel_working_orders_for_position",
                        lambda db, u, a, ad, p, side=None, reason="": calls.append(("sells", side)) or [uuid.uuid4()])
    monkeypatch.setattr("app.services.discord_channel_settings.for_guard", lambda db, uid, g: _ts())
    holds = []
    monkeypatch.setattr("app.services.ladder_hold.hold", lambda gid, seconds=20: holds.append(gid) or True)
    monkeypatch.setattr("app.services.ladder_hold.clear", lambda gid: holds.append(("cleared", gid)))
    yield SimpleNamespace(db=db, user=user, acct=acct, guard_id=guard.id, calls=calls, holds=holds, pos=pos)
    db.rollback()
    for model, oid in ((DiscordPositionGuard, guard.id), (DiscordAlertSource, src.id),
                       (BrokerAccount, acct.id), (User, user.id)):
        row = db.get(model, oid)
        if row is not None:
            db.delete(row)
            db.commit()
    db.close()


def _reapply(w, symbol="QQQ261023C00716000"):
    return api.reapply_exit_ladder(
        broker_symbol=symbol, request=SimpleNamespace(headers={}, client=None),
        broker_account_id=w.acct.id, db=w.db, user=w.user,
    )


def test_the_endpoint_clears_every_sell_and_restarts_the_ladder(world):
    out = _reapply(world)
    from app.models.order import OrderSide

    assert world.calls == ["ladder released", ("sells", OrderSide.SELL)], "sells only — resting buys stay"
    g = world.db.get(DiscordPositionGuard, world.guard_id)
    assert (g.sell_count, g.tp_off, g.entry_price, g.stop_price) == (0, False, D("1.03"), D("0.66"))
    assert D(out["stop"]) == D("0.66") and out["cancelled"] == 1
    assert world.holds == [world.guard_id, ("cleared", world.guard_id)], "held while it worked, then released"


def test_a_position_taken_over_by_hand_goes_back_to_its_channel(world, monkeypatch):
    """Reapply on a Self / taken-over position hands it back ("auto": the
    channel whose alert opened it) and restarts the ladder."""
    monkeypatch.setattr(guards, "is_manual", lambda db, g: True)
    _reapply(world)
    g = world.db.get(DiscordPositionGuard, world.guard_id)
    assert g.source_id is None and g.sell_count == 0 and g.stop_price == D("0.66")


def test_a_position_never_on_a_ladder_is_refused(world):
    from fastapi import HTTPException

    world.pos.option_strike = D("999")
    world.pos.broker_symbol = "QQQ261023C00999000"
    with pytest.raises(HTTPException) as e:
        _reapply(world, "QQQ261023C00999000")
    assert e.value.status_code == 409 and world.calls == []


# ── Manual takeover ──────────────────────────────────────────────────────────

def _takeover(w, symbol="QQQ261023C00716000"):
    return api.manual_takeover(
        broker_symbol=symbol, request=SimpleNamespace(headers={}, client=None),
        broker_account_id=w.acct.id, db=w.db, user=w.user,
    )


def test_manual_takeover_cancels_everything_and_hands_the_position_over(world, monkeypatch):
    handed = []
    monkeypatch.setattr(guards, "hand_to_trader",
                        lambda db, u, g, cancel: handed.append(g.id) or 2)
    world.db.get(DiscordPositionGuard, world.guard_id)      # loaded, so the change below is tracked
    out = _takeover(world)
    assert handed == [world.guard_id]
    g = world.db.get(DiscordPositionGuard, world.guard_id)
    assert guards.is_manual(world.db, g), "assigned to Self: the ladder no longer acts on it"
    assert ("sells", None) in world.calls, "every order on the contract — buys too, not only sells"
    assert out["cancelled"] == 3 and out["ladder"] is True
    assert world.holds == [world.guard_id, ("cleared", world.guard_id)]


def test_manual_takeover_with_no_ladder_still_clears_the_orders(world):
    world.pos.option_strike = D("999")
    world.pos.broker_symbol = "QQQ261023C00999000"
    out = _takeover(world, "QQQ261023C00999000")
    assert out["ladder"] is False and ("sells", None) in world.calls


def test_manual_takeover_clears_a_bracket_on_the_entry(world):
    from app.models.order import Order, OrderSide, OrderStatus, OrderType

    entry = Order(id=uuid.uuid4(), user_id=world.user.id, broker_account_id=world.acct.id,
                  instrument_type=InstrumentType.OPTION, symbol="QQQ", option_strike=D("716"),
                  option_right=OptionRight.CALL, option_expiry=EXP, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=D(9), status=OrderStatus.FILLED,
                  filled_quantity=D(9), stop_loss_price=D("0.40"), take_profit_price=D("2.00"))
    world.db.add(entry)
    world.db.commit()
    try:
        out = _takeover(world)
        world.db.refresh(entry)
        assert (entry.stop_loss_price, entry.take_profit_price) == (None, None)
        assert out["brackets_cleared"] == 1
    finally:
        world.db.delete(entry)
        world.db.commit()
