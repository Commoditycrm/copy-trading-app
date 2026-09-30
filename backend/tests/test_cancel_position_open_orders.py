"""The Positions row's "Canc.Open Ord" cancels THAT position's open orders.

It used to call cancel-all-open and sweep every open order on the account.
"""
import inspect
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import positions as api
from app.models.order import InstrumentType, OptionRight
from app.services import discord_position_guard as guards

USER = uuid.uuid4()


def _pos():
    return SimpleNamespace(
        symbol="SPY", broker_symbol="SPY261016P00500000", quantity=Decimal(4),
        instrument_type=InstrumentType.OPTION, option_strike=Decimal("500"),
        option_right=OptionRight.PUT, option_expiry=date(2026, 10, 16),
    )


def _call(monkeypatch, orders, guard=None):
    import app.api.trades as trades

    seen = {}
    monkeypatch.setattr(api, "adapter_for", lambda a, c: SimpleNamespace(get_positions=lambda: [_pos()]))
    monkeypatch.setattr(api, "decrypt_json", lambda b: {})
    monkeypatch.setattr(api, "_position_open_orders", lambda db, uid, acct_id, pos, st: orders)
    monkeypatch.setattr(trades, "_cancel_orders_batch",
                        lambda db, req, bg, user, os_, subs, via: seen.update(orders=os_, subs=subs, via=via)
                        or {"cancelled_count": len(os_), "failed_count": 0, "failed": []})
    monkeypatch.setattr(guards, "find", lambda *a, **k: guard)
    db = SimpleNamespace(get=lambda m, pk: SimpleNamespace(id=pk, user_id=USER, encrypted_credentials=b""),
                         commit=lambda: None)
    out = api.cancel_position_open_orders(
        "SPY261016P00500000", SimpleNamespace(headers={}, client=None), SimpleNamespace(),
        broker_account_id=uuid.uuid4(), include_subscribers=True, db=db, user=SimpleNamespace(id=USER),
    )
    return out, seen


def test_only_this_positions_orders_are_cancelled(monkeypatch):
    mine = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    out, seen = _call(monkeypatch, mine)
    assert seen["orders"] == mine and seen["subs"] is True
    assert seen["via"] == "cancel-position-open"
    assert out["cancelled_count"] == 2


def test_the_selection_is_one_contract_on_one_account():
    src = inspect.getsource(api._position_open_orders)
    for clause in ("Order.user_id == user_id", "Order.broker_account_id == acct_id",
                   "Order.symbol == pos.symbol", "Order.option_expiry.is_not_distinct_from",
                   "Order.option_strike.is_not_distinct_from", "Order.option_right.is_not_distinct_from"):
        assert clause in src, clause


def test_a_cancelled_ladder_stop_is_not_put_back(monkeypatch):
    stop = SimpleNamespace(id=uuid.uuid4())
    g = SimpleNamespace(stop_order_id=stop.id, stop_price=Decimal("1.50"))
    _call(monkeypatch, [stop], guard=g)
    assert g.stop_order_id is None and g.stop_price is None


def test_the_row_no_longer_calls_cancel_all():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "components",
                           "OpenPositionsTable.tsx")).read()
    assert "/cancel-open?broker_account_id=" in ui
    assert "cancel-all-open" not in ui
