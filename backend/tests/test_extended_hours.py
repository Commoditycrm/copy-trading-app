"""Tests for extended-hours (pre/post-market) order routing on Alpaca.

The EHGO bug: the trader (Webull) filled pre-market, but the subscriber's mirror
was a plain MARKET order on Alpaca, which can't fill in extended hours — it sat
queued until 09:30, and a SELL on top of that stuck BUY was wash-trade-rejected.

Fix: pre/post-market on Alpaca, `_to_immediate_close` routes a MARKETABLE LIMIT
with `extended_hours=True` instead of MARKET, so it fills like a market order.

Standalone: ``.venv/bin/python tests/test_extended_hours.py`` or under pytest.
"""
import os
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import market_hours as mh
import app.services.copy_engine as ce
from app.brokers.base import BrokerOrderRequest
from app.models.order import InstrumentType, OrderSide, OrderType

ET = ZoneInfo("America/New_York")
# 2026-07-23 is a Thursday (weekday).
def _et(h, m):
    return datetime(2026, 7, 23, h, m, tzinfo=ET)


# ── market_hours windows ──────────────────────────────────────────────────────

def test_in_extended_hours_windows():
    assert mh.in_extended_hours(_et(5, 36)) is True    # pre-market (the EHGO time)
    assert mh.in_extended_hours(_et(4, 0)) is True     # pre-market open edge
    assert mh.in_extended_hours(_et(9, 29)) is True     # just before the open
    assert mh.in_extended_hours(_et(9, 30)) is False    # regular open
    assert mh.in_extended_hours(_et(12, 0)) is False    # midday regular
    assert mh.in_extended_hours(_et(16, 0)) is True     # post-market open edge
    assert mh.in_extended_hours(_et(19, 59)) is True    # post-market
    assert mh.in_extended_hours(_et(20, 0)) is False    # post-market close edge
    assert mh.in_extended_hours(_et(3, 0)) is False     # before pre-market
    # Weekend (2026-07-25 = Saturday) is never extended hours.
    assert mh.in_extended_hours(datetime(2026, 7, 25, 5, 36, tzinfo=ET)) is False


def test_in_regular_session():
    assert mh.in_regular_session(_et(10, 0)) is True
    assert mh.in_regular_session(_et(5, 36)) is False
    assert mh.in_regular_session(_et(17, 0)) is False


# ── marketable stock limit pricing ────────────────────────────────────────────

class _AlpacaLike:
    """Passes the isinstance(AlpacaAdapter) check by subclassing it lazily."""
    def __init__(self, last):
        self._last = last
    def get_stock_latest_price(self, symbol):
        return self._last


def _mk_stock(side, qty="376"):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="EHGO", side=side,
        order_type=OrderType.MARKET, quantity=Decimal(qty), is_closing=False,
    )


def test_marketable_limit_prices_through_last():
    a = _AlpacaLike(Decimal("4.00"))
    assert ce._marketable_stock_limit(a, _mk_stock(OrderSide.BUY)) == Decimal("4.04")   # up
    assert ce._marketable_stock_limit(a, _mk_stock(OrderSide.SELL)) == Decimal("3.96")  # down
    assert ce._marketable_stock_limit(_AlpacaLike(None), _mk_stock(OrderSide.BUY)) is None


# ── _to_immediate_close routing ───────────────────────────────────────────────

def test_stock_extended_hours_routes_ext_limit(monkeypatched=None):
    """Pre-market on Alpaca → marketable LIMIT + extended_hours=True."""
    ce._alpaca_extended_hours = lambda adapter: True   # force the ext-hours branch
    out = ce._to_immediate_close(_AlpacaLike(Decimal("4.00")), _mk_stock(OrderSide.BUY))
    assert out.order_type == OrderType.LIMIT
    assert out.extended_hours is True
    assert out.limit_price == Decimal("4.04")


def test_stock_regular_hours_stays_market():
    """Regular hours (or non-Alpaca) → plain MARKET, unchanged behavior."""
    ce._alpaca_extended_hours = lambda adapter: False
    out = ce._to_immediate_close(_AlpacaLike(Decimal("4.00")), _mk_stock(OrderSide.SELL))
    assert out.order_type == OrderType.MARKET
    assert out.extended_hours is False
    assert out.limit_price is None


def test_non_forced_limit_mirror_gets_ext_hours_flag():
    """A plain (non-forced) stock LIMIT mirror pre-market on Alpaca is flagged
    extended_hours so it can actually fill, instead of resting until 09:30."""
    from datetime import datetime, timezone
    from app.brokers.base import BrokerOrderResult
    from app.models.order import OrderStatus

    ce._alpaca_extended_hours = lambda adapter: True
    placed = {}

    class _Adapter:
        def place_order(self, req):
            placed["req"] = req
            return BrokerOrderResult(
                broker_order_id="x", status=OrderStatus.SUBMITTED,
                submitted_at=datetime.now(timezone.utc),
            )

    class _Item:
        trader_filled = False          # not forced → mirror the trader's limit as-is
        adapter = _Adapter()
        request = BrokerOrderRequest(
            instrument_type=InstrumentType.STOCK, symbol="EHGO", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=Decimal("376"),
            limit_price=Decimal("4.00"), is_closing=False,
        )

    ce._place_mirror_with_conflict_resolve(_Item())
    assert placed["req"].order_type == OrderType.LIMIT
    assert placed["req"].extended_hours is True


if __name__ == "__main__":
    # Preserve/restore the monkeypatched helper so ordering doesn't matter.
    _orig = ce._alpaca_extended_hours
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"PASS  {name}")
    finally:
        ce._alpaca_extended_hours = _orig
    print("\nAll extended-hours tests passed.")
