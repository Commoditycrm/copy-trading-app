"""An exit alert cancels the entry that never filled.

The +10% retry gives an unfilled Discord entry one more chance. If it still
doesn't fill and an EXIT alert then arrives, that resting order is a bid for a
position the trader is already leaving — and it can still fill later, buying
into a move whose exit signal has already been given, with no rung of the
ladder left to protect it.

The subscribers' mirrors rest on the same stale bid, so the cancel cascades.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_execution as ex
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType

EXP = date(2026, 12, 18)


def _payload():
    return SimpleNamespace(
        symbol="MSFT", instrument_type=InstrumentType.OPTION,
        option_expiry=EXP, option_strike=Decimal("100"),
        option_right=OptionRight.CALL,
    )


def _order(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=None, parent_order_id=None, symbol="MSFT",
        instrument_type=InstrumentType.OPTION, option_expiry=EXP,
        option_strike=Decimal("100"), option_right=OptionRight.CALL,
        side=OrderSide.BUY, is_closing=False, order_type=OrderType.LIMIT,
        quantity=Decimal(4), filled_quantity=Decimal(0),
        status=OrderStatus.SUBMITTED, broker_order_id="brk-1",
        broker_account_id=uuid.uuid4(), closed_at=None,
    )
    base.update(kw)
    return Order(**base)


class _DB:
    """Returns a fixed set of orders from the entry SELECT."""

    def __init__(self, orders, acct=None):
        self._orders = orders
        self._acct = acct or SimpleNamespace(encrypted_credentials="x")
        self.commits = 0

    def execute(self, stmt):
        orders = list(self._orders)
        return SimpleNamespace(scalars=lambda: iter(orders))

    def get(self, model, key):
        return self._acct

    def commit(self):
        self.commits += 1


@pytest.fixture
def broker(monkeypatch):
    cancelled = []
    monkeypatch.setattr(ex, "adapter_for",
                        lambda a, c: SimpleNamespace(cancel_order=cancelled.append))
    monkeypatch.setattr(ex, "decrypt_json", lambda c: {})
    return cancelled


def _run(orders, broker_calls=None):
    user = SimpleNamespace(id=uuid.uuid4())
    for o in orders:
        o.user_id = user.id
    return ex.cancel_unfilled_entries(_DB(orders), user, _payload()), orders


def test_an_unfilled_entry_is_cancelled(broker):
    ids, orders = _run([_order()])
    assert ids == [orders[0].id]
    assert orders[0].status is OrderStatus.CANCELED
    assert orders[0].closed_at is not None
    assert broker == ["brk-1"]


def test_a_partially_filled_entry_is_left_alone(broker):
    """It is a real position now — that belongs to the trim ladder, which is
    about to run on it. Cancelling would not remove the position anyway."""
    ids, orders = _run([_order(filled_quantity=Decimal(2))])
    assert ids == []
    assert orders[0].status is OrderStatus.SUBMITTED
    assert broker == []


def test_a_local_only_entry_is_still_marked_cancelled(broker):
    """Never reached a broker, so there is nothing to cancel there — but it
    must not keep looking live to us."""
    ids, orders = _run([_order(broker_order_id=None)])
    assert ids == [orders[0].id]
    assert orders[0].status is OrderStatus.CANCELED
    assert broker == []


def test_a_broker_refusal_still_marks_it_cancelled(monkeypatch):
    """Already gone at the broker is the usual reason, and it means what we
    wanted. Leaving the row live would make us re-try it forever."""
    def _raises(a, c):
        return SimpleNamespace(
            cancel_order=lambda oid: (_ for _ in ()).throw(RuntimeError("gone"))
        )

    monkeypatch.setattr(ex, "adapter_for", _raises)
    monkeypatch.setattr(ex, "decrypt_json", lambda c: {})
    ids, orders = _run([_order()])
    assert ids == [orders[0].id]
    assert orders[0].status is OrderStatus.CANCELED


def test_nothing_to_cancel_does_not_commit(broker):
    db = _DB([])
    assert ex.cancel_unfilled_entries(db, SimpleNamespace(id=uuid.uuid4()), _payload()) == []
    assert db.commits == 0


def test_the_exit_path_cascades_the_cancel_to_mirrors():
    """The subscribers are resting on the same stale bid."""
    import inspect

    from app.api.discord_sources import _execute_signal

    src = inspect.getsource(_execute_signal)
    block = src[src.index("cancel_unfilled_entries"):][:1200]
    # The CALL, not merely the name — an import line mentions it too, which is
    # enough to satisfy a substring check while cascading nothing.
    assert "add_task(_run_cancel_fanout_in_background, _oid)" in block
    assert "_run_cancel_fanout_in_background(_oid)" in block


def test_the_exit_itself_survives_a_cancel_failure():
    """Getting OUT is the point of the alert; a stray resting entry is the
    lesser problem."""
    import inspect

    from app.api.discord_sources import _execute_signal

    src = inspect.getsource(_execute_signal)
    block = src[src.index("cancel_unfilled_entries"):]
    assert "except Exception" in block[:1200]
