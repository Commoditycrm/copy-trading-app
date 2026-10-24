"""Did the sell actually go through?

A trim cancels the stops that reserve its contracts, sells, and then puts a
stop back on what is left. The last step must not run until the sell is known
to have filled: the reconciler sizes the stop from what the BROKER reports, so
a stop placed mid-settlement covers the whole position and reserves the very
contracts the sell is taking — which the broker refuses.

The order row alone cannot answer it. A market exit is often FILLED by the time
placement returns, but when it is not the row says SUBMITTED for a sell that
goes through a moment later, and the remainder then sits unprotected until the
next poll tick. So it is read back from the broker, more than once.
"""
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.order import OrderStatus
from app.services import exit_confirm

ACCT = uuid.uuid4()


class _DB:
    def __init__(self, acct=None):
        self._acct = acct if acct is not None else SimpleNamespace(
            id=ACCT, encrypted_credentials="x")
        self.flushed = 0

    def get(self, _model, _id):
        return self._acct

    def flush(self):
        self.flushed += 1


def _order(status=OrderStatus.SUBMITTED, boid="B1"):
    return SimpleNamespace(id=uuid.uuid4(), status=status, broker_order_id=boid,
                           filled_quantity=Decimal(0))


@pytest.fixture
def broker(monkeypatch):
    """Stub the adapter; `reads` is what each successive get_order returns."""
    state = {"reads": [], "calls": 0}

    class _Adapter:
        def get_order(self, _boid):
            i = state["calls"]
            state["calls"] += 1
            result = state["reads"][min(i, len(state["reads"]) - 1)]
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr("app.brokers.adapter_for", lambda *a, **k: _Adapter())
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda *a, **k: {})
    return state


def _result(status="filled", qty="3"):
    return SimpleNamespace(status=status, filled_quantity=Decimal(qty))


def _confirm(db, order, broker=None, attempts=2):
    return exit_confirm.confirm_filled(db, ACCT, order, attempts=attempts,
                                       pause_s=0, sleep=lambda _s: None)


def test_an_order_already_filled_costs_no_broker_call(broker):
    broker["reads"] = [RuntimeError("must not be called")]
    assert _confirm(_DB(), _order(status=OrderStatus.FILLED)) is True
    assert broker["calls"] == 0


def test_a_fill_reported_after_placement_is_confirmed(broker):
    broker["reads"] = [_result()]
    order = _order()
    assert _confirm(_DB(), order, broker) is True
    assert order.status is OrderStatus.FILLED and order.filled_quantity == Decimal(3)


def test_it_checks_twice_before_giving_up(broker):
    """The whole point: one read a fraction of a second after placement is the
    case that reports 'not filled' for a sell that fills immediately after."""
    broker["reads"] = [_result(status="accepted", qty="0"), _result(qty="5")]
    order = _order()
    assert _confirm(_DB(), order, broker) is True
    assert broker["calls"] == 2 and order.filled_quantity == Decimal(5)


def test_an_unfilled_sell_is_not_claimed_as_filled(broker):
    broker["reads"] = [_result(status="accepted", qty="0")]
    order = _order()
    assert _confirm(_DB(), order, broker) is False
    assert order.status is OrderStatus.SUBMITTED, "an unfilled order was marked filled"
    assert broker["calls"] == 2, "it should have used both attempts"


def test_a_broker_error_is_not_a_fill(broker):
    """Never raises, and never optimistic — the stop falls back to the next
    poll tick, which is the behaviour that was there before."""
    broker["reads"] = [RuntimeError("503 from the broker")]
    order = _order()
    assert _confirm(_DB(), order, broker) is False
    assert order.status is OrderStatus.SUBMITTED


def test_a_partial_fill_counts_as_gone(broker):
    """Contracts left the position, so the stop must be re-sized around what
    is actually left rather than waiting."""
    broker["reads"] = [_result(status="partially_filled", qty="2")]
    order = _order()
    assert _confirm(_DB(), order, broker) is True
    assert order.filled_quantity == Decimal(2)


@pytest.mark.parametrize("order", [None, _order(boid=None), _order(boid="")])
def test_nothing_to_ask_about_is_not_a_fill(order, broker):
    broker["reads"] = [_result()]
    assert _confirm(_DB(), order, broker) is False
    assert broker["calls"] == 0


def test_the_fill_is_recorded_so_the_next_stop_is_sized_from_it(broker):
    broker["reads"] = [_result(qty="4")]
    db, order = _DB(), _order()
    assert _confirm(db, order, broker) is True
    assert db.flushed == 1


def test_a_status_only_partial_fill_still_counts(broker):
    """Some brokers report the state without a usable quantity on the first
    read. Contracts have still left the position, so the stop must be re-sized
    rather than left covering a holding that no longer exists."""
    broker["reads"] = [SimpleNamespace(status="partially_filled")]
    order = _order()
    assert _confirm(_DB(), order, broker) is True
    assert order.status is OrderStatus.FILLED
