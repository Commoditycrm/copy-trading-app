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


def _signal(**kw):
    """What the ALERT said — not a resolved contract. The realistic exit alert
    ("$MSFT 100c +25%") names no expiry at all."""
    base = dict(action="SELL", symbol="MSFT", asset_type="OPTION",
                strike="100", option_type="CALL", expiration=EXP.isoformat())
    base.update(kw)
    return base


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
        self.last_stmt = None

    def execute(self, stmt):
        self.last_stmt = stmt
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


def _run(orders, signal=None):
    user = SimpleNamespace(id=uuid.uuid4())
    for o in orders:
        o.user_id = user.id
    ids = ex.cancel_stale_entries_for_signal(_DB(orders), user, signal or _signal())
    return ids, orders


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
    user = SimpleNamespace(id=uuid.uuid4())
    assert ex.cancel_stale_entries_for_signal(db, user, _signal()) == []
    assert db.commits == 0


def test_a_buy_alert_cancels_nothing(broker):
    """Only an EXIT supersedes a resting entry. A BUY is the entry."""
    ids, orders = _run([_order()], _signal(action="BUY"))
    assert ids == []
    assert orders[0].status is OrderStatus.SUBMITTED
    assert broker == []


def test_the_filters_narrow_to_only_what_the_alert_stated():
    """The point of the whole change. A trim alert normally states symbol,
    strike and right and NOTHING else — resolve() completes it from the open
    position, which by definition does not exist when the entry never filled.
    A query that also pinned expiry could never match the order it exists to
    cancel, so each unstated field must WIDEN the match, not require NULL."""
    seen = {}

    def _spy(db, user, **kw):
        seen.update(kw)
        return []

    import app.services.discord_execution as mod
    real = mod.cancel_unfilled_entries
    mod.cancel_unfilled_entries = _spy
    try:
        mod.cancel_stale_entries_for_signal(
            _DB([]), SimpleNamespace(id=uuid.uuid4()),
            _signal(expiration=None),
        )
    finally:
        mod.cancel_unfilled_entries = real

    assert seen["symbol"] == "MSFT"
    assert seen["strike"] == Decimal("100")
    assert seen["right"] is OptionRight.CALL
    assert seen["expiry"] is None            # unstated → unconstrained


def _where_sql(signal):
    db = _DB([])
    ex.cancel_stale_entries_for_signal(db, SimpleNamespace(id=uuid.uuid4()), signal)
    # The WHERE clause alone — the SELECT list names every column regardless,
    # which would make any of these assertions pass for free.
    return str(db.last_stmt.whereclause)


def test_an_unstated_field_is_left_out_of_the_query_entirely():
    """Not merely passed as None. ``option_expiry == NULL`` is never true in
    SQL and ``IS NULL`` matches only stocks, so either one would silently
    return nothing for the exact alert this feature is for."""
    sql = _where_sql(_signal(expiration=None))
    assert "option_expiry" not in sql
    assert "option_strike" in sql and "option_right" in sql   # these WERE stated


def test_a_stated_field_does_constrain_the_query():
    """The widening must not go so far that a trim cancels an unrelated
    contract the alert never mentioned."""
    sql = _where_sql(_signal())
    assert "option_expiry" in sql


def test_a_stock_alert_does_not_constrain_the_option_columns():
    sql = _where_sql(_signal(asset_type="STOCK"))
    assert "option_expiry" not in sql
    assert "option_strike" not in sql
    assert "option_right" not in sql


def test_an_alert_without_an_expiry_still_cancels_the_entry(broker):
    """The live NVDA failure. "$NVDA 225c +25%" arrived while the 09/25 entry
    was still working and unfilled; nothing was cancelled."""
    ids, orders = _run([_order()], _signal(expiration=None))
    assert ids == [orders[0].id]
    assert orders[0].status is OrderStatus.CANCELED
    assert broker == ["brk-1"]


def test_the_cancel_runs_even_when_the_alert_cannot_be_resolved(monkeypatch):
    """The ordering IS the fix. An exit alert with no expiry and no open
    position makes resolve() refuse — so a cancel placed after it can never
    run in the one case it was written for."""
    import app.api.discord_sources as ds

    calls = []
    fanned = []
    monkeypatch.setattr(ds.discord_execution, "cancel_stale_entries_for_signal",
                        lambda db, user, sig: (calls.append(sig), ["oid-1"])[1])

    def _refuse(db, user, signal, sizing):
        raise ds.discord_execution.ExecutionRefused("you hold no matching position")

    monkeypatch.setattr(ds.discord_execution, "resolve", _refuse)

    import app.api.trades as trades
    monkeypatch.setattr(trades, "_run_cancel_fanout_in_background", fanned.append)

    msg = SimpleNamespace(
        id=uuid.uuid4(), order_id=None, status=None,
        status_reason=None, parsed_signal=_signal(expiration=None),
    )
    db = SimpleNamespace(get=lambda *a: None, commit=lambda: None)
    ds._execute_signal(db, SimpleNamespace(id=uuid.uuid4()), msg, None, None)

    assert calls, "the stale-entry cancel never ran"
    assert fanned == ["oid-1"], "the subscribers' mirrors were left resting"
    assert msg.status_reason == "you hold no matching position"


def test_the_exit_itself_survives_a_cancel_failure(monkeypatch):
    """Getting OUT is the point of the alert; a stray resting entry is the
    lesser problem, so a throwing cancel must not stop the order."""
    import app.api.discord_sources as ds

    def _boom(db, user, sig):
        raise RuntimeError("broker down")

    monkeypatch.setattr(ds.discord_execution, "cancel_stale_entries_for_signal", _boom)
    reached = []

    def _resolve(db, user, signal, sizing):
        reached.append(signal)
        raise ds.discord_execution.ExecutionRefused("stop here")

    monkeypatch.setattr(ds.discord_execution, "resolve", _resolve)

    msg = SimpleNamespace(id=uuid.uuid4(), order_id=None, status=None,
                          status_reason=None, parsed_signal=_signal())
    db = SimpleNamespace(get=lambda *a: None, commit=lambda: None)
    ds._execute_signal(db, SimpleNamespace(id=uuid.uuid4()), msg, None, None)
    assert reached, "a failing cancel stopped the exit"
