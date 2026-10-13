"""Cancelling one of our resting orders records what the broker says happened.

A cancel refused because the order already FILLED used to be recorded as a
cancel, which hid a real sell (QA 2026-10-13, QQQ 743P).
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.order import OrderStatus
from app.services.exit_confirm import cancel_and_record

AT = datetime(2026, 10, 13, 16, 59, 4, tzinfo=timezone.utc)


def _order():
    return SimpleNamespace(id=uuid.uuid4(), broker_order_id="b1", status=OrderStatus.SUBMITTED,
                           quantity=Decimal(6), filled_quantity=Decimal(0), filled_avg_price=None,
                           broker_filled_at=None, closed_at=None)


class _Adapter:
    def __init__(self, cancel, order=None):
        self._cancel, self._order, self.reads = cancel, order, 0

    def cancel_order(self, _boid):
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return self._cancel

    def get_order(self, _boid):
        self.reads += 1
        if isinstance(self._order, Exception):
            raise self._order
        return self._order


def _res(status, qty=0, px=None):
    return SimpleNamespace(status=status, filled_quantity=Decimal(qty),
                           filled_avg_price=px, filled_at=AT if qty else None)


def test_an_accepted_cancel_is_a_cancel_and_costs_no_read():
    o, a = _order(), _Adapter(True)
    cancel_and_record(a, o)
    assert o.status == OrderStatus.CANCELED and a.reads == 0


def test_a_cancel_refused_because_it_filled_is_recorded_as_the_fill():
    o = _order()
    cancel_and_record(_Adapter(False, _res(OrderStatus.FILLED, 6, Decimal("0.92"))), o)
    assert o.status == OrderStatus.FILLED
    assert (o.filled_quantity, o.filled_avg_price, o.broker_filled_at) == (Decimal(6), Decimal("0.92"), AT)


def test_a_failed_cancel_on_an_order_still_working_stays_working():
    """Marking it cancelled would be a lie: it still reserves the contracts."""
    o = _order()
    cancel_and_record(_Adapter(RuntimeError("timeout"), _res(OrderStatus.SUBMITTED)), o)
    assert o.status == OrderStatus.SUBMITTED


def test_a_failed_cancel_that_had_filled_is_recorded_as_the_fill():
    o = _order()
    cancel_and_record(_Adapter(RuntimeError("timeout"), _res(OrderStatus.FILLED, 6, Decimal("0.92"))), o)
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == Decimal("0.92")


def test_an_unreadable_broker_falls_back_to_cancelled():
    o = _order()
    cancel_and_record(_Adapter(False, RuntimeError("rate limited")), o)
    assert o.status == OrderStatus.CANCELED


def test_already_cancelled_at_the_broker_is_a_cancel():
    o = _order()
    cancel_and_record(_Adapter(False, _res(OrderStatus.CANCELED)), o)
    assert o.status == OrderStatus.CANCELED


def test_an_order_never_sent_is_just_cancelled():
    o = _order()
    o.broker_order_id = None
    a = _Adapter(True)
    cancel_and_record(a, o)
    assert o.status == OrderStatus.CANCELED and a.reads == 0


# ── the same fix on every path the ladder cancels through ────────────────────
# A Discord TRADER's ladder and a Discord SUBSCRIBER's ladder run the same code:
# the poller's ladder pass (pnl_poller._enforce_discord_trailing_stops, reached
# from both role branches) cancels through _make_stop_canceller, and alerts,
# Positions buttons, Self and AI trim cancel through
# discord_sources._cancel_stop_order. Both must record a fill that beat the cancel.

import inspect

import pytest

from app.models.user import UserRole


def _filled_adapter():
    return _Adapter(False, _res(OrderStatus.FILLED, 6, Decimal("0.92")))


def test_the_poller_canceller_records_a_fill():
    from app.services import pnl_poller
    o = _order()
    db = SimpleNamespace(get=lambda model, key: o)
    pnl_poller._make_stop_canceller(db, _filled_adapter())(o.id)
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == Decimal("0.92")


@pytest.mark.parametrize("role", [UserRole.TRADER, UserRole.SUBSCRIBER])
def test_the_alert_path_canceller_records_a_fill_for_either_role(monkeypatch, role):
    import app.api.discord_sources as ds
    import app.brokers as brokers
    import app.services.crypto as crypto
    from app.models.broker_account import BrokerAccount

    o = _order()
    o.broker_account_id = uuid.uuid4()
    acct = SimpleNamespace(id=o.broker_account_id, encrypted_credentials="x")
    db = SimpleNamespace(get=lambda model, key: acct if model is BrokerAccount else o)
    monkeypatch.setattr(brokers, "adapter_for", lambda a, c: _filled_adapter())
    monkeypatch.setattr(crypto, "decrypt_json", lambda v: {})
    user = SimpleNamespace(id=uuid.uuid4(), role=role)

    ds._cancel_stop_order(db, user)(o.id)
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == Decimal("0.92")


def test_both_role_branches_run_the_same_ladder_pass():
    from app.services import pnl_poller
    trader = inspect.getsource(pnl_poller._enforce_one_trader)
    subscriber = inspect.getsource(pnl_poller._enforce_one_inner)
    assert "_enforce_discord_trailing_stops(" in trader
    assert "_enforce_discord_trailing_stops(" in subscriber
    sweep = inspect.getsource(pnl_poller._enforce_discord_trailing_stops)
    assert "_make_stop_canceller(" in sweep


def test_a_take_profit_that_fills_as_close_is_pressed_is_a_fill(monkeypatch):
    """The close's own cancel of what rests on the contract, for either role."""
    from app.api import positions as mod

    o = _order()
    o.order_type = SimpleNamespace(value="limit")

    class _Db:
        def execute(self, _q):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [o]))
        def commit(self): pass
        def refresh(self, _o): pass

    published = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.events, "publish", lambda uid, evt: published.append(evt))
    monkeypatch.setattr(mod.copy_engine, "_order_event", lambda kind, order: {"type": kind})
    pos = SimpleNamespace(instrument_type=None, symbol="QQQ", option_expiry=None,
                          option_strike=None, option_right=None, broker_symbol="QQQ")
    got = mod._cancel_working_orders_for_position(
        _Db(), SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4()),
        _filled_adapter(), pos)
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == Decimal("0.92")
    assert got == [o.id], "a fill changed the position, so the close must re-read it"
    assert published == [{"type": "order.updated"}]
