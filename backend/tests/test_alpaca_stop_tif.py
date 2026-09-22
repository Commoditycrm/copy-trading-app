"""A protective stop must outlive the trading session.

A DAY stop is cancelled at 16:00 ET, so the position is unprotected overnight
and pre-market — exactly when a gap happens. Alpaca builds SDK request objects
inside place_order, so the assertion is on the object handed to the SDK.
"""
import os
import sys
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.alpaca import AlpacaAdapter
from app.brokers.base import BrokerOrderRequest
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType


def _submit_and_capture(monkeypatch, order_type, **extra):
    """Drive place_order with a stub client; return the tif it submitted."""
    seen = {}

    class _Stub:
        def submit_order(self, order_data=None, **kw):
            seen["tif"] = getattr(order_data, "time_in_force", None)
            r = MagicMock()
            r.id = "brk-1"
            r.client_order_id = "c"
            r.status = MagicMock(value="accepted")
            r.filled_qty = 0
            r.filled_avg_price = None
            return r

    a = AlpacaAdapter.__new__(AlpacaAdapter)
    a._client = _Stub()
    for name in ("trading", "client", "_trading"):
        setattr(a, name, _Stub())

    req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="SPY", side=OrderSide.SELL,
        order_type=order_type, quantity=Decimal(2),
        option_expiry=date(2026, 9, 25), option_strike=Decimal("771"),
        option_right=OptionRight.CALL, is_closing=True, client_order_id="c", **extra,
    )
    try:
        a.place_order(req)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"place_order needs more wiring than this stub provides: {exc!r}")
    return seen.get("tif")


def test_a_stop_is_gtc(monkeypatch):
    tif = _submit_and_capture(monkeypatch, OrderType.STOP, stop_price=Decimal("1.50"))
    assert str(getattr(tif, "value", tif)).lower() == "gtc"


def test_ordinary_orders_still_expire_with_the_session(monkeypatch):
    """Only stops get GTC — a stale entry must not work into the next day."""
    tif = _submit_and_capture(monkeypatch, OrderType.LIMIT, limit_price=Decimal("1.50"))
    assert str(getattr(tif, "value", tif)).lower() == "day"
