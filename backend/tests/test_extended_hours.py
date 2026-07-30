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


# ── option close routing (market-first on Alpaca in RTH) ──────────────────────
# Regression for prod NVDA C195 SELL (2026-07-29): the trader closed at MARKET but
# the Alpaca subscriber got a marketable LIMIT priced off a stale bid ($0.55),
# which rested above the market and was cancelled unfilled. Alpaca DOES accept
# option market orders in RTH, so we now send MARKET there (fills like the trader).

from datetime import date as _date
from app.models.order import OptionRight


class _OptAdapter:
    """Fake option adapter exposing the quote hook _marketable_option_limit needs."""
    def __init__(self, bid=Decimal("0.30"), ask=Decimal("0.34")):
        self._bid, self._ask = bid, ask
    def get_option_latest_quote(self, occ):
        return (self._bid, self._ask)


def _mk_option(side=OrderSide.SELL, order_type=OrderType.MARKET):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="NVDA", side=side,
        order_type=order_type, quantity=Decimal("10"),
        option_expiry=_date(2026, 7, 31), option_strike=Decimal("195"),
        option_right=OptionRight.CALL, is_closing=True,
    )


def test_option_close_alpaca_regular_hours_is_market():
    """Alpaca option close in regular hours → MARKET (fills like the trader)."""
    ce._alpaca_regular_session = lambda adapter: True
    out = ce._to_immediate_close(_OptAdapter(), _mk_option())
    assert out.order_type == OrderType.MARKET
    assert out.limit_price is None


def test_option_close_offhours_or_non_alpaca_is_marketable_limit():
    """Outside RTH / non-Alpaca → marketable LIMIT priced through the bid (SELL)."""
    ce._alpaca_regular_session = lambda adapter: False
    out = ce._to_immediate_close(_OptAdapter(bid=Decimal("0.30")), _mk_option(OrderSide.SELL))
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price is not None and out.limit_price > 0


def test_option_market_no_quote_detector():
    assert ce._option_market_no_quote('{"code":40310000,"message":"no available quote"}') is True
    assert ce._option_market_no_quote("insufficient qty available") is False
    assert ce._option_market_no_quote("account not eligible to trade uncovered") is False


def test_option_market_no_quote_falls_back_to_marketable_limit():
    """An Alpaca option MARKET refused for 'no available quote' retries as a
    marketable LIMIT instead of failing — so the close still fills."""
    import uuid
    from datetime import datetime, timezone
    from app.brokers.base import BrokerOrderResult
    from app.models.order import OrderStatus

    calls = []

    class _Adapter(_OptAdapter):
        def place_order(self, req):
            calls.append(req.order_type)
            if req.order_type == OrderType.MARKET:
                raise RuntimeError('{"code":40310000,"message":"no available quote"}')
            return BrokerOrderResult(
                broker_order_id="ok", status=OrderStatus.SUBMITTED,
                submitted_at=datetime.now(timezone.utc),
            )

    class _Item:
        trader_filled = False        # skip the forced-close block; exercise the place fallback
        adapter = _Adapter()
        subscriber_user_id = uuid.uuid4()
        broker_account_id = uuid.uuid4()
        child_order_id = uuid.uuid4()
        request = _mk_option(OrderSide.SELL, OrderType.MARKET)

    res = ce._place_mirror_with_conflict_resolve(_Item())
    assert calls == [OrderType.MARKET, OrderType.LIMIT]   # market refused → limit fallback
    assert res.broker_order_id == "ok"


# ── extended-hours entry priced off the TRADER's fill, not our stale quote ─────
# Regression for prod STFS BUY (2026-07-29, pre-market): trader filled at $4.95,
# but our last-trade quote was ~$3.09, so the subscriber's buy limit landed at
# $3.12 — below the market — and never filled. Fix: anchor the extended-hours
# limit to the trader's fill × (1 ± cap). Regular hours is unaffected (MARKET).

def test_ext_hours_limit_anchors_to_trader_fill_not_local_quote():
    """Pre-market BUY: limit is trader_fill × (1 + cap), NOT our stale last-trade."""
    ce._alpaca_extended_hours = lambda adapter: True
    # Our local quote is a stale $3.09; the trader actually filled at $4.95.
    out = ce._to_immediate_close(
        _AlpacaLike(Decimal("3.09")), _mk_stock(OrderSide.BUY),
        trader_ref_price=Decimal("4.95"),
    )
    assert out.order_type == OrderType.LIMIT
    assert out.extended_hours is True
    # 4.95 × 1.03 = 5.0985 → 5.09 (rounded down), well above the stale 3.12.
    assert out.limit_price == Decimal("5.09")


def test_ext_hours_sell_anchors_below_trader_fill():
    """Pre-market SELL: limit is trader_fill × (1 − cap) so it's marketable."""
    ce._alpaca_extended_hours = lambda adapter: True
    out = ce._to_immediate_close(
        _AlpacaLike(Decimal("9.00")), _mk_stock(OrderSide.SELL),
        trader_ref_price=Decimal("5.00"),
    )
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price == Decimal("4.85")   # 5.00 × 0.97


def test_ext_hours_falls_back_to_local_quote_without_anchor():
    """No trader fill price → fall back to the local marketable-limit (unchanged)."""
    ce._alpaca_extended_hours = lambda adapter: True
    out = ce._to_immediate_close(_AlpacaLike(Decimal("4.00")), _mk_stock(OrderSide.BUY))
    assert out.order_type == OrderType.LIMIT
    assert out.limit_price == Decimal("4.04")   # 4.00 × 1.01, the old behavior


def test_regular_hours_entry_still_market_with_anchor_ignored():
    """Regular hours: still a MARKET order — the anchor must NOT turn it into a
    limit (the option-close market fix must stay intact)."""
    ce._alpaca_extended_hours = lambda adapter: False
    out = ce._to_immediate_close(
        _AlpacaLike(Decimal("3.09")), _mk_stock(OrderSide.BUY),
        trader_ref_price=Decimal("4.95"),
    )
    assert out.order_type == OrderType.MARKET
    assert out.limit_price is None


if __name__ == "__main__":
    # Preserve/restore the monkeypatched helpers so ordering doesn't matter.
    _orig = ce._alpaca_extended_hours
    _orig_rs = ce._alpaca_regular_session
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"PASS  {name}")
    finally:
        ce._alpaca_extended_hours = _orig
        ce._alpaca_regular_session = _orig_rs
    print("\nAll extended-hours tests passed.")
