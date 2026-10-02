"""POST /api/trades/{order_id}/re-enter — buy back what a filled close took off.

The Positions page's "Closed today" table calls it with the quantity and limit
price from the row. It must build the buy from the CLOSED order's contract and
account, and refuse what can't sensibly be re-entered.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import trades
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.schemas.order import ReEnterIn
from app.services import market_hours

USER = SimpleNamespace(id=uuid.uuid4())
ACCT = SimpleNamespace(id=uuid.uuid4(), user_id=USER.id, connection_status="connected")


def _closed(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=USER.id, hidden_at=None, side=OrderSide.SELL,
        filled_quantity=D("4"), instrument_type=InstrumentType.OPTION, symbol="SPY",
        option_expiry=market_hours.now_et().date(), option_strike=D("764"),
        option_right=OptionRight.CALL, broker_account_id=ACCT.id,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _DB:
    def __init__(self, order, acct=ACCT, connected=None):
        self.order, self.acct, self.connected = order, acct, connected

    def get(self, model, _id):
        return self.order if model is trades.Order else self.acct

    def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self.connected))


@pytest.fixture
def placed(monkeypatch):
    calls = []

    def fake(db, user, payload, account_id, background, request, **kw):
        calls.append((payload, account_id, kw))
        return "placed"

    monkeypatch.setattr(trades, "_place_trader_order", fake)
    return calls


def _re_enter(db, qty="2", price="1.10"):
    return trades.re_enter_trade(
        order_id=db.order.id, payload=ReEnterIn(quantity=D(qty), limit_price=D(price)),
        request=None, background=None, db=db, user=USER,
    )


def test_buys_the_closed_contract_back_at_the_limit(placed):
    src = _closed()
    assert _re_enter(_DB(src)) == "placed"
    payload, account_id, kw = placed[0]
    assert (payload.side, payload.order_type) == (OrderSide.BUY, OrderType.LIMIT)
    assert (payload.quantity, payload.limit_price) == (D("2"), D("1.10"))
    assert (payload.symbol, payload.option_strike, payload.option_right, payload.option_expiry) == (
        "SPY", D("764"), OptionRight.CALL, src.option_expiry)
    assert account_id == ACCT.id
    assert kw == {}                       # an ordinary entry: not a close, no fan-out override


def test_stock_close_re_enters_as_a_stock_buy(placed):
    src = _closed(instrument_type=InstrumentType.STOCK, symbol="AAPL",
                  option_expiry=None, option_strike=None, option_right=None)
    _re_enter(_DB(src), qty="2.5", price="230.15")
    payload = placed[0][0]
    assert payload.instrument_type == InstrumentType.STOCK and payload.option_expiry is None
    assert payload.quantity == D("2.5")


def test_uses_the_connected_account_when_the_original_is_gone(placed):
    other = SimpleNamespace(id=uuid.uuid4(), user_id=USER.id, connection_status="connected")
    _re_enter(_DB(_closed(), acct=None, connected=other))
    assert placed[0][1] == other.id


@pytest.mark.parametrize("src, qty, code", [
    (_closed(user_id=uuid.uuid4()), "2", 404),                      # someone else's order
    (_closed(filled_quantity=D("0")), "2", 409),                    # never filled
    (_closed(side=OrderSide.BUY), "2", 422),                        # a buy-to-close: would sell to open
    (_closed(option_expiry=date.today() - timedelta(days=7)), "2", 422),   # expired contract
    (_closed(), "1.5", 422),                                        # fractional contracts
])
def test_refusals(placed, src, qty, code):
    with pytest.raises(HTTPException) as exc:
        _re_enter(_DB(src), qty=qty)
    assert exc.value.status_code == code
    assert placed == []


def test_no_connected_broker(placed):
    with pytest.raises(HTTPException) as exc:
        _re_enter(_DB(_closed(), acct=None, connected=None))
    assert exc.value.status_code == 409 and placed == []
