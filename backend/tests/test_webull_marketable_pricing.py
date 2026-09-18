"""Forced mirror orders must be made FILLABLE on direct Webull too.

Two defects, both of which left a direct-Webull subscriber resting an order that
could not trade while the trader was already out of the position:

  #5 Extended hours. `_to_immediate_close` only re-routed a pre/post-market stock
     order to a flagged marketable LIMIT when the adapter was an `AlpacaAdapter`
     — an isinstance check. Direct Webull needs it just as much: its MARKET
     orders are pinned to `support_trading_session=CORE` (WebullAdapter._session),
     so a forced MARKET mirror just queues for 09:30. The check is now the
     adapter capability `requires_extended_hours_limit`, so it covers any broker
     that gates the session at the order level, and still excludes
     aggregator-routed accounts that trade extended hours natively.

  #6 Option pricing. `_marketable_option_limit` returned the order UNCHANGED when
     the adapter exposed no `get_option_latest_quote` — keeping the TRADER's
     limit price on the subscriber's order. WebullAdapter had no quote method at
     all, so that was not a rare fallback; it was every forced option close.
     There is now a second price source: the trader's own fill ±
     `mirror_option_close_slippage_pct`.

Offline: fake adapters, no SDK, no network.
"""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce
from app.brokers.base import BrokerAdapter, BrokerOrderRequest
from app.brokers.snaptrade import SnapTradeAdapter
from app.brokers.webull import WebullAdapter
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType


# ── adapter doubles ─────────────────────────────────────────────────────────
class _WebullLike(BrokerAdapter):
    """Direct Webull's shape: gates the session at the order level, and (by
    default) has NO option quote API — the state of the adapter before #6, and
    still the live state whenever Webull's market-data entitlement is absent."""
    name = "webull"
    requires_extended_hours_limit = True

    def __init__(self, last_price=None):
        super().__init__({})
        self._last = last_price

    def verify_connection(self): ...
    def place_order(self, req): ...
    def get_order(self, boid): ...

    def get_stock_latest_price(self, symbol):
        return self._last


class _WebullWithQuotes(_WebullLike):
    """Same, but market data IS entitled."""
    def __init__(self, bid=None, ask=None, last_price=None):
        super().__init__(last_price)
        self._bid, self._ask = bid, ask

    def get_option_latest_quote(self, occ):
        return (self._bid, self._ask)


class _AggregatorLike(BrokerAdapter):
    """SnapTrade-shaped: the upstream broker owns the session, so a MARKET order
    trades extended hours natively and must NOT be re-routed."""
    name = "snaptrade"
    requires_extended_hours_limit = False

    def __init__(self):
        super().__init__({})

    def verify_connection(self): ...
    def place_order(self, req): ...
    def get_order(self, boid): ...


def _stock(side=OrderSide.SELL, limit=Decimal("10.00")):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=side,
        order_type=OrderType.LIMIT, quantity=Decimal("5"), limit_price=limit,
        is_closing=True,
    )


def _option(side=OrderSide.SELL, limit=Decimal("1.00")):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=side,
        order_type=OrderType.LIMIT, quantity=Decimal("2"), limit_price=limit,
        option_expiry=date(2026, 6, 19), option_strike=Decimal("220"),
        option_right=OptionRight.CALL, is_closing=True,
    )


class _ForceExtHours:
    """Pin the clock question so these tests don't depend on wall time."""
    def __init__(self, extended: bool):
        self._ext = extended

    def __enter__(self):
        from app.services import market_hours
        self._saved = market_hours.in_extended_hours
        market_hours.in_extended_hours = lambda *a, **k: self._ext
        return self

    def __exit__(self, *exc):
        from app.services import market_hours
        market_hours.in_extended_hours = self._saved


# ── #5: the capability flag replaces the isinstance check ───────────────────
def test_real_adapters_declare_the_capability_correctly():
    """The flag is what the routing now keys on, so the values matter."""
    assert WebullAdapter.requires_extended_hours_limit is True
    assert SnapTradeAdapter.requires_extended_hours_limit is False
    from app.brokers.alpaca import AlpacaAdapter
    assert AlpacaAdapter.requires_extended_hours_limit is True


def test_needs_ext_hours_limit_is_true_for_webull_in_extended_hours():
    with _ForceExtHours(True):
        assert ce._needs_extended_hours_limit(_WebullLike()) is True


def test_needs_ext_hours_limit_is_false_in_regular_session():
    with _ForceExtHours(False):
        assert ce._needs_extended_hours_limit(_WebullLike()) is False


def test_needs_ext_hours_limit_is_false_for_an_aggregator():
    """SnapTrade's upstream broker trades extended hours itself — re-routing
    would make the order MISS, not fill."""
    with _ForceExtHours(True):
        assert ce._needs_extended_hours_limit(_AggregatorLike()) is False


def test_webull_premarket_stock_close_becomes_a_flagged_limit():
    """The regression. Before, this returned a plain MARKET order, which Webull
    pins to the CORE session — it could not trade until 09:30."""
    with _ForceExtHours(True):
        out = ce._to_immediate_close(
            _WebullLike(), _stock(OrderSide.SELL), trader_ref_price=Decimal("100.00"),
        )
    assert out.order_type == OrderType.LIMIT
    assert out.extended_hours is True
    # Anchored below the trader's fill so a SELL is marketable: 100 × (1 − 3%).
    assert out.limit_price == Decimal("97.00")


def test_webull_premarket_stock_buy_anchors_above_trader_fill():
    with _ForceExtHours(True):
        out = ce._to_immediate_close(
            _WebullLike(), _stock(OrderSide.BUY), trader_ref_price=Decimal("100.00"),
        )
    assert out.order_type == OrderType.LIMIT and out.extended_hours is True
    assert out.limit_price == Decimal("103.00")


def test_webull_regular_hours_stock_close_still_goes_market():
    """Regular session is unchanged — a MARKET order is the right thing there."""
    with _ForceExtHours(False):
        out = ce._to_immediate_close(
            _WebullLike(), _stock(OrderSide.SELL), trader_ref_price=Decimal("100.00"),
        )
    assert out.order_type == OrderType.MARKET
    assert out.limit_price is None and out.extended_hours is False


def test_aggregator_premarket_stock_close_still_goes_market():
    with _ForceExtHours(True):
        out = ce._to_immediate_close(
            _AggregatorLike(), _stock(OrderSide.SELL), trader_ref_price=Decimal("100.00"),
        )
    assert out.order_type == OrderType.MARKET


def test_ext_hours_falls_back_to_market_when_unpriceable():
    """No trader anchor and no local quote — MARKET is no worse than before."""
    with _ForceExtHours(True):
        out = ce._to_immediate_close(_WebullLike(last_price=None), _stock(), None)
    assert out.order_type == OrderType.MARKET


# ── #6: option pricing without a quote API ──────────────────────────────────
def test_option_close_without_a_quote_is_priced_off_the_trader_fill():
    """The regression. Before, no quote method meant the order came back
    UNCHANGED — still carrying the TRADER's limit — and rested unfilled."""
    out = ce._marketable_option_limit(
        _WebullLike(), _option(OrderSide.SELL, limit=Decimal("1.00")),
        trader_ref_price=Decimal("2.00"),
    )
    assert out.order_type == OrderType.LIMIT
    # 2.00 × (1 − 5%) = 1.90 — marketable, and NOT the trader's stale 1.00.
    assert out.limit_price == Decimal("1.90")
    assert out.limit_price != Decimal("1.00")


def test_option_buy_close_anchors_above_the_trader_fill():
    out = ce._marketable_option_limit(
        _WebullLike(), _option(OrderSide.BUY), trader_ref_price=Decimal("2.00"),
    )
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price == Decimal("2.10")   # 2.00 × 1.05


def test_a_real_quote_beats_the_trader_anchor():
    """When market data IS entitled, the live book wins — the anchor is only a
    fallback, never a replacement for a real price."""
    out = ce._marketable_option_limit(
        _WebullWithQuotes(bid=Decimal("3.00"), ask=Decimal("3.20")),
        _option(OrderSide.SELL), trader_ref_price=Decimal("2.00"),
    )
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price == Decimal("3.00")   # the bid, not 2.00 × 0.95


def test_empty_book_falls_through_to_the_trader_anchor():
    """An adapter that HAS the quote method but returns nothing (one-sided book,
    quote outage, un-entitled app_key) must still get a fillable price."""
    out = ce._marketable_option_limit(
        _WebullWithQuotes(bid=None, ask=None),
        _option(OrderSide.SELL), trader_ref_price=Decimal("2.00"),
    )
    assert out.order_type == OrderType.LIMIT and out.limit_price == Decimal("1.90")


def test_no_quote_and_no_anchor_leaves_the_order_untouched():
    """Nothing to price from — returning the order as-is is no worse than not
    rewriting, which is the documented contract."""
    req = _option(OrderSide.SELL, limit=Decimal("1.00"))
    out = ce._marketable_option_limit(_WebullLike(), req, trader_ref_price=None)
    assert out is req


def test_incomplete_contract_terms_are_left_alone():
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=Decimal("1"), limit_price=Decimal("1"),
    )
    assert ce._marketable_option_limit(_WebullLike(), req, Decimal("2.00")) is req


def test_to_immediate_close_routes_webull_options_through_the_anchor():
    """End to end: a forced option close on Webull (never an Alpaca regular
    session) comes out as a marketable limit, not the trader's price."""
    out = ce._to_immediate_close(
        _WebullLike(), _option(OrderSide.SELL, limit=Decimal("1.00")),
        trader_ref_price=Decimal("2.00"),
    )
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price == Decimal("1.90")


def test_anchor_percent_is_configurable():
    from app.config import get_settings
    assert get_settings().mirror_option_close_slippage_pct > 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull marketable-pricing tests passed.")
