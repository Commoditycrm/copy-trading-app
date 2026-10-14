"""Trl.Stop on an option rests ONE trailing stop at the broker for every contract.

QA 2026-10-14, SPY 773P: 2 held, the ladder's take-profit pair holding 1 of
them with its own fixed linked stop. Trl.Stop armed an app-watched slice (5% off
1.375, exit 1.3063) beside all that — checked once per 15s poll and sold at
market only after cancelling the pair. It sold at 1.21, 23s later.

Now Trl.Stop takes over the exit: the take-profit pair is released and stays
off, the slice trail is cleared, and a real STOP rests at the broker for all
contracts, raised by the poller as the price makes new highs.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.api import positions as mod
from app.brokers.base import BrokerPosition
from app.models.order import InstrumentType, OptionRight
from app.services import discord_position_guard as guards
from app.services import discord_stop_orders, discord_trailing_stop

_SYM = "SPY261014P00773000"


def _pos(qty="2"):
    return BrokerPosition(
        broker_symbol=_SYM, symbol="SPY", instrument_type=InstrumentType.OPTION,
        quantity=Decimal(qty), avg_entry_price=Decimal("1.07"), current_price=Decimal("1.375"),
        market_value=None, unrealized_pnl=None,
        option_expiry=date(2026, 10, 14), option_strike=Decimal("773"), option_right=OptionRight.PUT,
    )


def _guard():
    return SimpleNamespace(
        id=uuid.uuid4(), symbol="SPY", option_strike=Decimal("773"), option_right="put",
        option_expiry=date(2026, 10, 14), closed_at=None, entry_price=Decimal("1.07"),
        sell_count=0, stop_price=Decimal("0.21"), stop_trail_pct=None, stop_peak=None,
        stop_order_id=uuid.uuid4(), tp_order_id=uuid.uuid4(), tp_stop_order_id=uuid.uuid4(),
        tp_rung=1, tp_qty=Decimal(1), tp_off=False, tp_backoff_until=None,
        trail_qty=None, trail_amount=None, peak_price=None, trail_percent=None, armed_at=None,
    )


@pytest.fixture
def arm(monkeypatch):
    released = []

    def _arm(guard, pos=None, mark="1.375", pct="5"):
        acct = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), connection_status="connected",
                               encrypted_credentials="x")
        user = SimpleNamespace(id=acct.user_id)
        db = SimpleNamespace(get=lambda model, key: acct, add=lambda o: None, commit=lambda: None)
        monkeypatch.setattr(mod, "adapter_for", lambda a, c: object())
        monkeypatch.setattr(mod, "decrypt_json", lambda v: {})
        monkeypatch.setattr(mod, "_held_for_protection", lambda adapter: [pos or _pos()])
        monkeypatch.setattr(mod.trailing_stop_close, "trailing_stop_supported", lambda a, p: False)
        monkeypatch.setattr(guards, "find", lambda *a, **k: guard)
        from app.services import discord_take_profit, live_marks
        import app.api.discord_sources as ds
        monkeypatch.setattr(live_marks, "position_mark", lambda p, uid: Decimal(mark))
        monkeypatch.setattr(ds, "_cancel_stop_order", lambda db, user: (lambda oid: None))

        def _release(db, g, cancel):
            released.append(g.id)
            g.tp_order_id = g.tp_stop_order_id = g.tp_rung = g.tp_qty = None
            return True

        monkeypatch.setattr(discord_take_profit, "release", _release)
        return mod.arm_trailing_stop(
            broker_symbol=_SYM, request=None, background=None, broker_account_id=acct.id,
            trail_percent=Decimal(pct), db=db, user=user,
        )

    _arm.released = released
    return _arm


def test_the_trailing_stop_is_a_real_stop_for_every_contract(arm):
    g = _guard()
    out = arm(g)
    assert out["mode"] == "broker_stop"
    assert g.stop_price == Decimal("1.30")            # 5% below 1.375, to the tick
    assert g.stop_trail_pct == Decimal("5") and g.stop_peak == Decimal("1.375")
    # Nothing earmarked away from it: the stop reconciler sizes it to all held.
    assert discord_stop_orders.desired_quantity(Decimal(2), g) == Decimal(2)


def test_it_takes_over_from_the_take_profit(arm):
    g = _guard()
    arm(g)
    assert arm.released == [g.id], "the take-profit pair must give its contracts back"
    assert g.tp_off is True, "and stay off, or it comes back next pass and splits the stop"
    assert g.tp_order_id is None and g.tp_qty is None


def test_the_app_watched_slice_is_gone(arm):
    g = _guard()
    g.trail_qty, g.trail_amount, g.peak_price = Decimal(2), Decimal("0.07"), Decimal("1.30")
    arm(g)
    assert (g.trail_qty, g.trail_amount, g.peak_price) == (None, None, None)


def test_the_poller_raises_it_as_the_price_climbs():
    g = _guard()
    g.stop_price, g.stop_trail_pct, g.stop_peak = Decimal("1.30"), Decimal("5"), Decimal("1.375")
    g.stop_order_id = uuid.uuid4()                    # resting at the broker
    g.tp_stop_order_id = None
    assert discord_trailing_stop.decide(g, Decimal("1.50"), Decimal(2)) is None
    assert g.stop_price == Decimal("1.42")            # 5% below the new high, to the tick
    assert g.stop_peak == Decimal("1.50")


def test_a_pullback_never_lowers_it():
    g = _guard()
    g.stop_price, g.stop_trail_pct, g.stop_peak = Decimal("1.42"), Decimal("5"), Decimal("1.50")
    discord_trailing_stop.decide(g, Decimal("1.45"), Decimal(2))
    assert g.stop_price == Decimal("1.42")


def test_a_short_is_refused(arm):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        arm(_guard(), pos=_pos("-2"))
    assert e.value.status_code == 422
