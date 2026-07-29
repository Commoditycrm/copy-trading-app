"""Regression test for the "protect the stop" guard.

Reproduces the prod incident (STKH, 28-Jul, Webull trader → Alpaca subscribers):
the trader rested BOTH a stop-loss (sell STOP @ 4.50) and a take-profit (sell
LIMIT @ 7.00) on the same 100-share position. On Alpaca a position's shares back
only ONE resting sell, so placing the take-profit mirror hit ``insufficient qty``
(40310000) — and the conflict-resolver cancelled the blocker, which was the
STOP, silently removing the subscriber's downside protection.

The fix: ``_working_protective_stop_ids`` finds the resting stop, and
``_place_mirror_with_conflict_resolve`` raises ``_KeptProtectiveStop`` (keeping
the stop, skipping the take-profit) instead of cancelling the stop — but only for
a NON-forced resting LIMIT close, so genuine exits and stop-vs-stop modifies are
unaffected.

Runs standalone (``.venv/bin/python tests/test_protect_stop_guard.py``) or under
pytest. Uses a real in-memory SQLite session against the actual ORM query — only
the ``orders`` table is created (audit_logs uses JSONB, which SQLite can't
render, and we don't need it here).
"""
import os
import sys
import uuid
from decimal import Decimal

# Allow running standalone — put the backend package root on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.copy_engine as ce
from app.brokers import BrokerOrderRequest
from app.models.broker_account import BrokerName
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

_ACCT = uuid.uuid4()  # the subscriber's Alpaca account — shared by stop + mirror


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)  # only the table under test; skip JSONB audit_logs
    return Session(eng)


def _add_stop(db, user_id, *, status=OrderStatus.SUBMITTED, has_broker_id=True,
              order_type=OrderType.STOP, acct=_ACCT):
    """A resting protective stop-loss (sell STOP) the subscriber already holds."""
    o = Order(
        id=uuid.uuid4(),
        user_id=user_id,
        broker_account_id=acct,
        instrument_type=InstrumentType.STOCK,
        symbol="STKH",
        side=OrderSide.SELL,
        order_type=order_type,
        quantity=Decimal("100"),
        stop_price=Decimal("4.50"),
        is_closing=True,
        status=status,
        filled_quantity=Decimal("0"),
        broker_order_id="stop-broker-id" if has_broker_id else None,
    )
    db.add(o)
    db.flush()
    return o


def _tp_item(user_id, *, order_type=OrderType.LIMIT, trader_filled=False, acct=_ACCT):
    """A _PendingMirror for the incoming take-profit (sell LIMIT) mirror."""
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK,
        symbol="STKH",
        side=OrderSide.SELL,
        order_type=order_type,
        quantity=Decimal("100"),
        limit_price=Decimal("7.00"),
        is_closing=True,
    )
    return ce._PendingMirror(
        child_order_id=uuid.uuid4(),
        subscriber_user_id=user_id,
        broker_account_id=acct,
        broker=BrokerName.ALPACA,
        adapter=object(),          # never touched by the read-only query
        request=req,
        trader_filled=trader_filled,
    )


def test_finds_working_stop_on_same_contract():
    """The STKH bug: a resting stop-loss is detected so the TP can be skipped."""
    db = _session()
    sub = uuid.uuid4()
    stop = _add_stop(db, sub)
    kept = ce._working_protective_stop_ids(_tp_item(sub), db=db)
    assert kept == [stop.id]


def test_ignores_non_stop_working_orders():
    """A resting LIMIT (another take-profit, not a stop) is NOT protected."""
    db = _session()
    sub = uuid.uuid4()
    _add_stop(db, sub, order_type=OrderType.LIMIT)  # a limit, not a protective stop
    assert ce._working_protective_stop_ids(_tp_item(sub), db=db) == []


def test_ignores_terminal_stops():
    """A stop that already filled/cancelled no longer reserves shares → not kept."""
    db = _session()
    sub = uuid.uuid4()
    _add_stop(db, sub, status=OrderStatus.CANCELED)
    assert ce._working_protective_stop_ids(_tp_item(sub), db=db) == []


def test_ignores_stop_that_never_reached_broker():
    """No broker_order_id → it isn't actually resting at Alpaca → not kept."""
    db = _session()
    sub = uuid.uuid4()
    _add_stop(db, sub, has_broker_id=False)
    assert ce._working_protective_stop_ids(_tp_item(sub), db=db) == []


def test_scoped_to_same_subscriber_and_account():
    """Another subscriber's / another account's stop must not be seen."""
    db = _session()
    sub = uuid.uuid4()
    _add_stop(db, uuid.uuid4())                    # different subscriber
    _add_stop(db, sub, acct=uuid.uuid4())          # same sub, different account
    assert ce._working_protective_stop_ids(_tp_item(sub), db=db) == []


def test_stop_type_stop_limit_also_protected():
    """A STOP_LIMIT stop-loss is protected too, not just plain STOP."""
    db = _session()
    sub = uuid.uuid4()
    stop = _add_stop(db, sub, order_type=OrderType.STOP_LIMIT)
    assert ce._working_protective_stop_ids(_tp_item(sub), db=db) == [stop.id]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll protect-the-stop guard tests passed.")
