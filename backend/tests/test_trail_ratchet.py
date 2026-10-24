"""The trailing stop follows a new high within a second or two, not 15-30s.

trail_ratchet raises a guard's trailing stop on Alpaca's live price (stream
cache only) and moves the RESTING stop in place at the broker, instead of
waiting for the P&L poller's pass and its cancel-then-place-next-pass.
"""
import os
import sys
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import trail_ratchet as tr

EXP = date(2026, 10, 14)


class _Adapter:
    supports_replace = True

    def __init__(self, fail=False, new_id=None):
        self.replaced, self.fail, self.new_id = [], fail, new_id

    def replace_order(self, boid, req):
        if self.fail:
            raise RuntimeError("broker said no")
        self.replaced.append((boid, req.stop_price, req.client_order_id))
        return SimpleNamespace(broker_order_id=self.new_id or boid, status=OrderStatus.SUBMITTED)


@pytest.fixture
def world(monkeypatch):
    """A user with a trailing stop at 1.35 (high 1.375 gives back 5% of the 0.305 profit over 1.07) resting at the broker."""
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.SUBSCRIBER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x", connection_status="connected")
    db.add(acct)
    db.flush()
    stop = Order(id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
                 instrument_type=InstrumentType.OPTION, symbol="SPY", option_strike=Decimal("773"),
                 option_right=OptionRight.PUT, option_expiry=EXP, side=OrderSide.SELL,
                 order_type=OrderType.STOP, quantity=Decimal(2), stop_price=Decimal("1.35"),
                 status=OrderStatus.SUBMITTED, is_closing=True, broker_order_id="B1")
    db.add(stop)
    db.flush()
    guard = DiscordPositionGuard(user_id=user.id, symbol="SPY", option_strike=Decimal("773"),
                                 option_right="put", option_expiry=EXP, sell_count=0,
                                 entry_price=Decimal("1.07"), stop_price=Decimal("1.35"),
                                 stop_trail_pct=Decimal("5"), stop_peak=Decimal("1.375"),
                                 stop_order_id=stop.id)
    db.add(guard)
    db.commit()

    state = SimpleNamespace(price=Decimal("1.375"), adapter=_Adapter(), woken=[],
                            user=user, acct=acct, guard_id=guard.id, stop_id=stop.id)
    monkeypatch.setattr(tr, "_live_price", lambda g: state.price)
    monkeypatch.setattr("app.brokers.adapter_for", lambda a, c: state.adapter)
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda v: {})
    monkeypatch.setattr("app.services.order_intent.mark_app_originated", lambda oid: None)
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    monkeypatch.setattr("app.services.pnl_poller.poll_now", lambda aid: state.woken.append(aid))
    tr._last_modify.clear()
    yield state

    db.rollback()
    for model, oid in ((DiscordPositionGuard, guard.id), (Order, stop.id),
                       (BrokerAccount, acct.id), (User, user.id)):
        row = db.get(model, oid)
        if row is not None:
            db.delete(row)
            db.commit()
    db.close()


def _read(state):
    with SessionLocal() as db:
        return db.get(DiscordPositionGuard, state.guard_id), db.get(Order, state.stop_id)


def test_a_new_high_moves_the_resting_stop_in_place(world):
    world.price = Decimal("1.50")
    out = tr.ratchet_one(world.guard_id)
    g, o = _read(world)
    assert g.stop_price == Decimal("1.47")                       # gives back 5% of the 0.43 profit from 1.50, to the tick
    assert o.stop_price == Decimal("1.47")
    assert [(b, s) for b, s, _ in world.adapter.replaced] == [("B1", Decimal("1.47"))]
    assert world.adapter.replaced[0][2] != str(world.stop_id), "the replacement needs its own client id"
    assert "1.35 -> 1.47" in out and "(moved)" in out


def test_no_new_high_does_nothing(world):
    world.price = Decimal("1.36")
    tr.ratchet_one(world.guard_id)
    g, o = _read(world)
    assert g.stop_price == o.stop_price == Decimal("1.35")
    assert world.adapter.replaced == []


def test_a_new_id_from_the_broker_is_kept(world):
    world.adapter = _Adapter(new_id="B2")                        # Alpaca opens a new order
    world.price = Decimal("1.50")
    tr.ratchet_one(world.guard_id)
    _, o = _read(world)
    assert o.broker_order_id == "B2"


def test_modifies_are_spaced_but_the_level_still_rises(world):
    world.price = Decimal("1.50")
    tr.ratchet_one(world.guard_id)
    world.price = Decimal("1.60")                                # a second high, at once
    tr.ratchet_one(world.guard_id)
    g, o = _read(world)
    assert g.stop_price == Decimal("1.57")
    assert o.stop_price == Decimal("1.47") and len(world.adapter.replaced) == 1
    tr._last_modify.clear()                                      # the gap has passed
    tr.ratchet_one(world.guard_id)
    _, o = _read(world)
    assert o.stop_price == Decimal("1.57") and len(world.adapter.replaced) == 2


def test_a_broker_without_in_place_replace_is_left_to_the_poller(world):
    world.adapter = SimpleNamespace(supports_replace=False)
    world.price = Decimal("1.50")
    tr.ratchet_one(world.guard_id)
    g, o = _read(world)
    assert g.stop_price == Decimal("1.47") and o.stop_price == Decimal("1.35")


def test_a_refused_modify_keeps_the_order_as_the_broker_has_it(world):
    world.adapter = _Adapter(fail=True)
    world.price = Decimal("1.50")
    tr.ratchet_one(world.guard_id)
    g, o = _read(world)
    assert g.stop_price == Decimal("1.47") and o.stop_price == Decimal("1.35")


def test_through_the_stop_with_nothing_resting_wakes_the_poller(world):
    with SessionLocal() as db:
        g = db.get(DiscordPositionGuard, world.guard_id)
        g.stop_order_id = None
        db.commit()
    world.price = Decimal("1.25")
    out = tr.ratchet_one(world.guard_id)
    assert world.woken == [world.acct.id] and "poller woken" in out


def test_a_user_the_poller_holds_is_skipped_this_second(world):
    world.price = Decimal("1.50")
    held = threading.Event()
    release = threading.Event()

    def _hold():
        with tr.user_lock(world.user.id):
            held.set()
            release.wait(5)

    t = threading.Thread(target=_hold)
    t.start()
    held.wait(5)
    try:
        tr.tick()
        g, _ = _read(world)
        assert g.stop_price == Decimal("1.35"), "must not write while the poller holds the user"
    finally:
        release.set()
        t.join()
    tr.tick()
    g, _ = _read(world)
    assert g.stop_price == Decimal("1.47")


def test_the_poller_s_discord_pass_holds_the_same_lock():
    import inspect
    from app.services import pnl_poller
    src = inspect.getsource(pnl_poller._enforce_discord_trailing_stops)
    assert "trail_ratchet.user_lock(acct.user_id)" in src
