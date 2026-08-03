"""Unit tests for SnapTrade equity reconstruction from open positions.

Regression for the cash-only-equity problem: many SnapTrade brokerages
report only CASH (no mark-to-market total), so today's P&L ignored open
positions and a daily loss/profit limit set to "total" never saw
unrealized moves. The fix sums each open position's market value so equity
= cash + Σ(market value).

Pure-logic tests — no live SnapTrade client. We build a bare adapter
instance and stub ``get_positions``.

Run standalone:  .venv/bin/python tests/test_snaptrade_positions_equity.py
Or under pytest: pytest tests/test_snaptrade_positions_equity.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.base import BrokerPosition
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.order import InstrumentType


def _adapter(positions=None, raises=False):
    """A bare adapter (no __init__) with get_positions stubbed."""
    a = object.__new__(SnapTradeAdapter)
    if raises:
        def _boom():
            raise RuntimeError("broker down")
        a.get_positions = _boom
    else:
        a.get_positions = lambda: list(positions or [])
    return a


def _stock(sym, qty, current_price, market_value):
    return BrokerPosition(
        broker_symbol=sym, symbol=sym, instrument_type=InstrumentType.STOCK,
        quantity=Decimal(str(qty)), avg_entry_price=None,
        current_price=None if current_price is None else Decimal(str(current_price)),
        market_value=None if market_value is None else Decimal(str(market_value)),
        unrealized_pnl=None,
    )


def _option(sym, qty, current_price, market_value):
    return BrokerPosition(
        broker_symbol=sym, symbol=sym, instrument_type=InstrumentType.OPTION,
        quantity=Decimal(str(qty)), avg_entry_price=None,
        current_price=None if current_price is None else Decimal(str(current_price)),
        market_value=None if market_value is None else Decimal(str(market_value)),
        unrealized_pnl=None,
    )


def test_sums_market_values():
    a = _adapter([_stock("AAPL", 10, 150, 1500), _stock("MSFT", 5, 400, 2000)])
    assert a._open_positions_market_value() == Decimal("3500")


def test_stock_falls_back_to_price_times_qty():
    a = _adapter([_stock("AAPL", 10, 150, None)])   # no market_value
    assert a._open_positions_market_value() == Decimal("1500")


def test_option_fallback_applies_100x_multiplier():
    a = _adapter([_option("AAPL", 3, Decimal("4.25"), None)])  # 3 × 4.25 × 100
    assert a._open_positions_market_value() == Decimal("1275.00")


def test_empty_account_is_zero():
    a = _adapter([])
    assert a._open_positions_market_value() == Decimal("0")


def test_fetch_failure_returns_none():
    a = _adapter(raises=True)
    assert a._open_positions_market_value() is None


def test_unvaluable_positions_return_none():
    """Positions exist but none can be valued → None (caller stays cash-only)."""
    a = _adapter([_stock("AAPL", 10, None, None)])
    assert a._open_positions_market_value() is None


def test_mixed_valued_and_unvaluable_sums_the_valued():
    a = _adapter([_stock("AAPL", 10, 150, 1500), _stock("XYZ", 1, None, None)])
    assert a._open_positions_market_value() == Decimal("1500")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll SnapTrade equity-reconstruction tests passed.")
