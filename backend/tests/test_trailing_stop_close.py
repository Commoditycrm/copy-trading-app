"""Tests for the Sell-All trailing-stop variation.

Covers the three pieces added: the capability decision
(services.trailing_stop_close), PlaceOrderIn validation, and the Alpaca adapter
building a native TrailingStopOrderRequest (client stubbed — no network).

Run standalone:  .venv/bin/python tests/test_trailing_stop_close.py
Or under pytest: pytest tests/test_trailing_stop_close.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.order import InstrumentType, OrderSide, OrderType
from app.schemas.order import PlaceOrderIn
from app.services import trailing_stop_close


class _StubAdapter:
    def __init__(self, supports): self.supports_trailing_stop = supports


class _StubPos:
    def __init__(self, instrument_type): self.instrument_type = instrument_type


_STOCK = _StubPos(InstrumentType.STOCK)
_OPT = _StubPos(InstrumentType.OPTION)


# ── capability decision ──────────────────────────────────────────────────────

def test_supported_broker_stock_is_supported():
    assert trailing_stop_close.trailing_stop_supported(_StubAdapter(True), _STOCK) is True

def test_supported_broker_option_is_not():
    assert trailing_stop_close.trailing_stop_supported(_StubAdapter(True), _OPT) is False

def test_unsupported_broker_stock_is_not():
    assert trailing_stop_close.trailing_stop_supported(_StubAdapter(False), _STOCK) is False

def test_adapter_without_flag_is_not():
    class Bare: pass
    assert trailing_stop_close.trailing_stop_supported(Bare(), _STOCK) is False


# ── PlaceOrderIn validation ──────────────────────────────────────────────────

def _base(**kw):
    d = dict(instrument_type=InstrumentType.STOCK, symbol="AAPL",
             side=OrderSide.SELL, order_type=OrderType.TRAILING_STOP, quantity=Decimal("1"))
    d.update(kw)
    return d

def test_trailing_with_percent_ok():
    PlaceOrderIn(**_base(trail_percent=Decimal("5")))

def test_trailing_with_price_ok():
    PlaceOrderIn(**_base(trail_price=Decimal("2.50")))

def test_trailing_needs_exactly_one():
    for kw in ({}, dict(trail_percent=Decimal("5"), trail_price=Decimal("2"))):
        try:
            PlaceOrderIn(**_base(**kw)); assert False, "expected validation error"
        except Exception:
            pass

def test_trail_rejected_on_non_trailing():
    try:
        PlaceOrderIn(**_base(order_type=OrderType.MARKET, trail_percent=Decimal("5")))
        assert False, "expected validation error"
    except Exception:
        pass


# ── Alpaca builds a native TrailingStopOrderRequest ──────────────────────────

def test_alpaca_builds_trailing_stop_request():
    from alpaca.trading.requests import TrailingStopOrderRequest
    from app.brokers.alpaca import AlpacaAdapter
    from app.brokers.base import BrokerOrderRequest

    class _Resp:
        id = "abc"; status = "accepted"; submitted_at = None
        filled_qty = 0; filled_avg_price = None; legs = []
    class _Client:
        def __init__(self): self.captured = None
        def submit_order(self, req): self.captured = req; return _Resp()

    a = AlpacaAdapter({"api_key": "k", "api_secret": "s", "paper": True})
    a._client = _Client()
    a.place_order(BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.TRAILING_STOP, quantity=Decimal("3"),
        trail_percent=Decimal("5"),
    ))
    assert isinstance(a._client.captured, TrailingStopOrderRequest)
    assert float(a._client.captured.trail_percent) == 5.0
    assert a.supports_trailing_stop is True

def test_alpaca_rejects_trailing_on_option():
    from app.brokers.alpaca import AlpacaAdapter
    from app.brokers.base import BrokerOrderRequest
    a = AlpacaAdapter({"api_key": "k", "api_secret": "s", "paper": True})
    try:
        a.place_order(BrokerOrderRequest(
            instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.SELL,
            order_type=OrderType.TRAILING_STOP, quantity=Decimal("1"),
            trail_percent=Decimal("5"),
        ))
        assert False, "expected ValueError for option trailing stop"
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok:", name)
    print("all trailing-stop tests passed")
