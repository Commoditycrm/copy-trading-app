"""Unit tests for the direct-Webull execution adapter (subscriber mirrors).

Covers the write path added so SUBSCRIBERS can execute mirror orders on a direct
Webull account (not only via SnapTrade): the Webull order-dict construction, the
open-vs-close (position_intent) mapping — the load-bearing SELL_TO_CLOSE fix — and
the get_order_detail response parsing. All offline: no SDK/network calls; the
order builders are pure, and the detail-parse path is driven by a fake SDK client.

Standalone:  .venv/bin/python tests/test_webull_adapter_orders.py
Or pytest:   pytest tests/test_webull_adapter_orders.py
"""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.base import BrokerOrderRequest
from app.brokers.webull import WebullAdapter
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _adapter() -> WebullAdapter:
    return WebullAdapter(
        {"app_key": "k", "app_secret": "s", "account_id": "ACC1", "region_id": "us"}
    )


# ── client_order_id ──────────────────────────────────────────────────────────
def test_client_order_id_is_stable_32_char_from_uuid():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("1"),
        client_order_id="11112222-3333-4444-5555-666677778888",
    )
    coid = a._client_order_id(req)
    assert coid == "11112222333344445555666677778888"  # dashes stripped
    assert len(coid) == 32                              # Webull's max
    assert a._client_order_id(req) == coid              # deterministic → idempotent


# ── stock orders ─────────────────────────────────────────────────────────────
def test_stock_market_buy_dict():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="aapl", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("3"),
    )
    d = a._build_stock_order(req, "c1")
    assert d["symbol"] == "AAPL" and d["market"] == "US"
    assert d["side"] == "BUY" and d["order_type"] == "MARKET"
    assert d["quantity"] == "3" and d["time_in_force"] == "DAY"
    assert d["support_trading_session"] == "CORE"   # market can't be extended
    assert "limit_price" not in d


def test_stock_limit_sell_extended_hours_uses_all_session():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=Decimal("10"),
        limit_price=Decimal("185.5"), extended_hours=True,
    )
    d = a._build_stock_order(req, "c2")
    assert d["side"] == "SELL" and d["order_type"] == "LIMIT"
    assert d["limit_price"] == "185.50"                # 2dp for >= $1
    assert d["support_trading_session"] == "ALL"       # extended-hours limit


# ── option orders + the open/close (position_intent) mapping ─────────────────
def _opt(side, is_closing, right=OptionRight.CALL, otype=OrderType.LIMIT):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=side,
        order_type=otype, quantity=Decimal("1"),
        limit_price=Decimal("0.45") if otype == OrderType.LIMIT else None,
        option_expiry=date(2026, 6, 19), option_strike=Decimal("220"),
        option_right=right, is_closing=is_closing,
    )


def test_option_open_intents():
    a = _adapter()
    assert a._position_intent(_opt(OrderSide.BUY, False)) == "BUY_TO_OPEN"
    assert a._position_intent(_opt(OrderSide.SELL, False)) == "SELL_TO_OPEN"


def test_option_close_intents_sell_to_close():
    """The regression that motivated this: a closing SELL must be SELL_TO_CLOSE,
    never SELL_TO_OPEN (Webull rejects the latter 'no position to close')."""
    a = _adapter()
    assert a._position_intent(_opt(OrderSide.SELL, True)) == "SELL_TO_CLOSE"
    assert a._position_intent(_opt(OrderSide.BUY, True)) == "BUY_TO_CLOSE"


def test_option_order_dict_carries_us_option_leg():
    a = _adapter()
    d = a._build_option_order(_opt(OrderSide.SELL, True, right=OptionRight.PUT,
                                   otype=OrderType.MARKET), "c4")
    assert d["position_intent"] == "SELL_TO_CLOSE" and d["side"] == "SELL"
    assert d["order_type"] == "MARKET" and d["option_strategy"] == "SINGLE"
    assert len(d["legs"]) == 1
    leg = d["legs"][0]
    # Category header derives from these — must be an OPTION/US leg.
    assert leg["instrument_type"] == "OPTION" and leg["market"] == "US"
    assert leg["option_type"] == "PUT" and leg["strike_price"] == "220.00"
    assert leg["option_expire_date"] == "2026-06-19"
    assert leg["position_intent"] == "SELL_TO_CLOSE"


def test_option_missing_terms_raises():
    a = _adapter()
    bad = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("1"),
    )
    try:
        a._build_option_order(bad, "c")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for option order without terms")


# ── formatting ───────────────────────────────────────────────────────────────
def test_price_precision_and_qty():
    a = _adapter()
    assert a._fmt_price(Decimal("0.455")) == "0.4550"   # 4dp for < $1
    assert a._fmt_price(Decimal("185.505")) == "185.51"  # 2dp for >= $1
    assert a._fmt_qty(Decimal("3.0")) == "3"             # whole-share tidy


# ── detail parsing (get_order / cancel status) via a fake SDK client ─────────
class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeOrderOps:
    def __init__(self, detail_resp):
        self._detail = detail_resp

    def get_order_detail(self, account_id, client_order_id):
        return self._detail


class _FakeTrade:
    def __init__(self, detail_resp):
        self.order_v3 = _FakeOrderOps(detail_resp)


def test_fetch_detail_parses_filled_option_leg():
    a = _adapter()
    body = {
        "order_id": "WB123", "client_order_id": "c9", "category": "US_OPTION",
        "items": [{
            "order_status": "FILLED", "filled_qty": "2", "filled_price": "0.51",
        }],
    }
    parsed = a._fetch_detail(_FakeTrade(_FakeResp(200, body)), "c9")
    assert parsed is not None
    _order, is_option, status, filled_qty, filled_px = parsed
    assert is_option is True
    assert status == OrderStatus.FILLED
    assert filled_qty == Decimal("2") and filled_px == Decimal("0.51")


def test_fetch_detail_partial_stock():
    a = _adapter()
    body = {"category": "US_STOCK",
            "items": [{"order_status": "PARTIAL_FILLED", "filled_qty": "1", "filled_price": "10"}]}
    _o, is_option, status, q, p = a._fetch_detail(_FakeTrade(_FakeResp(200, body)), "c")
    assert is_option is False and status == OrderStatus.PARTIALLY_FILLED
    assert q == Decimal("1") and p == Decimal("10")


def test_fetch_detail_none_on_non_200():
    a = _adapter()
    assert a._fetch_detail(_FakeTrade(_FakeResp(500, None)), "c") is None


def test_get_order_maps_status():
    a = _adapter()
    body = {"category": "US_STOCK", "items": [{"order_status": "SUBMITTED", "filled_qty": "0"}]}

    class _T:
        order_v3 = _FakeOrderOps(_FakeResp(200, body))

    # monkeypatch the cached client so get_order uses our fake trade
    a._trade_client = lambda: _T()  # type: ignore[method-assign]
    res = a.get_order("c")
    assert res.status == OrderStatus.SUBMITTED
    assert res.broker_order_id == "c" and res.filled_quantity == Decimal("0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull-adapter order tests passed.")
