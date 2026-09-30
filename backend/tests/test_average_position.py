"""Average (add to) a held position from the Positions row.

The mirror of close_position: an option at market goes out as a limit through
the ask, it rides the trader's normal order path, and a Discord ladder follows
the new cost basis only when the add is BELOW its entry.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import positions as api
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.schemas.position import AveragePositionIn
from app.services import discord_position_guard as guards

USER = uuid.uuid4()


def _pos(qty="4", mark="2.00", option=True):
    return SimpleNamespace(
        symbol="SPY", broker_symbol="SPY261016P00500000", quantity=Decimal(qty),
        instrument_type=InstrumentType.OPTION if option else InstrumentType.STOCK,
        option_strike=Decimal("500") if option else None,
        option_right=OptionRight.PUT if option else None,
        option_expiry=date(2026, 10, 16) if option else None,
        current_price=Decimal(mark),
    )


@pytest.fixture
def placed(monkeypatch):
    return _wire(monkeypatch)


def _wire(monkeypatch, pos=None, guard=None):
    sent = []
    pos = pos or _pos()
    monkeypatch.setattr(api, "adapter_for", lambda acct, creds: SimpleNamespace(get_positions=lambda: [pos]))
    monkeypatch.setattr(api, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(api, "_option_close_limit", lambda adapter, p, side: Decimal("2.10") if side == OrderSide.BUY else None)
    monkeypatch.setattr(api, "_place_trader_order",
                        lambda db, user, payload, acct_id, bg, req, **kw: sent.append(payload) or SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(guards, "find", lambda *a, **k: guard)
    return sent


def _call(payload):
    db = SimpleNamespace(
        get=lambda model, pk: SimpleNamespace(user_id=USER, encrypted_credentials=b"", connection_status="connected", id=pk),
        commit=lambda: None,
    )
    return api.average_position("SPY261016P00500000", payload, SimpleNamespace(headers={}, client=None),
                                SimpleNamespace(), broker_account_id=uuid.uuid4(), db=db,
                                user=SimpleNamespace(id=USER))


def test_an_option_at_market_buys_through_the_ask(placed):
    _call(AveragePositionIn(quantity=Decimal(2)))
    (o,) = placed
    assert (o.side, o.order_type, o.limit_price, o.quantity) == (OrderSide.BUY, OrderType.LIMIT, Decimal("2.10"), Decimal(2))


def test_a_limit_average_uses_the_trader_price(placed):
    _call(AveragePositionIn(quantity=Decimal(1), order_type=OrderType.LIMIT, limit_price=Decimal("1.80")))
    assert placed[0].limit_price == Decimal("1.80")


def test_a_stock_at_market_stays_a_market_order(monkeypatch):
    sent = _wire(monkeypatch, pos=_pos(qty="10", option=False))
    _call(AveragePositionIn(quantity=Decimal(5)))
    assert sent[0].order_type == OrderType.MARKET and sent[0].option_strike is None


def test_shorts_are_refused(monkeypatch):
    _wire(monkeypatch, pos=_pos(qty="-4"))
    with pytest.raises(api.HTTPException) as exc:
        _call(AveragePositionIn(quantity=Decimal(1)))
    assert exc.value.status_code == 422


def test_averaging_down_moves_the_ladder_entry(monkeypatch):
    g = SimpleNamespace(symbol="SPY", entry_price=Decimal("3.00"))
    _wire(monkeypatch, guard=g)          # buys at the 2.10 ask
    _call(AveragePositionIn(quantity=Decimal(4)))
    assert g.entry_price == Decimal("2.55")      # (4 x 3.00 + 4 x 2.10) / 8


def test_averaging_up_leaves_the_ladder_entry(monkeypatch):
    g = SimpleNamespace(symbol="SPY", entry_price=Decimal("2.00"))
    _wire(monkeypatch, guard=g)          # 2.10 is above entry
    _call(AveragePositionIn(quantity=Decimal(4)))
    assert g.entry_price == Decimal("2.00")
