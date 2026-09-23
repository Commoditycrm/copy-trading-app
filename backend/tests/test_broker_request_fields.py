"""Every field a persisted Order carries has to reach the broker request.

A field that is simply ABSENT from that translation fails silently: the
request dataclass defaults it, the order still places, and only the BROKER's
behaviour is wrong. ``is_closing`` was absent for exactly this reason, and it
cost a day of rejected exits on a live account.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.trades import broker_request_for
from app.brokers.webull import WebullAdapter
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType


class _Order:
    def __init__(self, is_closing=False, side=OrderSide.SELL):
        self.id = uuid.uuid4()
        self.instrument_type = InstrumentType.OPTION
        self.symbol = "SPY"
        self.side = side
        self.order_type = OrderType.MARKET
        self.quantity = Decimal(3)
        self.limit_price = self.stop_price = None
        self.trail_percent = self.trail_price = None
        self.take_profit_price = self.stop_loss_price = None
        self.option_expiry = date(2026, 9, 22)
        self.option_strike = Decimal("775")
        self.option_right = OptionRight.CALL
        self.is_closing = is_closing


def test_a_closing_order_reaches_the_broker_as_closing():
    """Open vs close is a distinct field on Webull and SnapTrade options -- it
    is NOT implied by the side."""
    assert broker_request_for(_Order(is_closing=True), False).is_closing is True


def test_an_entry_is_not_marked_closing():
    assert broker_request_for(_Order(is_closing=False), False).is_closing is False


def test_a_closing_option_sell_goes_out_as_sell_to_close():
    """End to end through the adapter. SELL_TO_OPEN against a contract already
    held is what Webull rejects with OPENAPI_POSITION_ORDER_INTENT_MISMATCH,
    "Close intent mismatches position direction" -- which blocked every Discord
    trim, protective stop and manual close on a live account."""
    req = broker_request_for(_Order(is_closing=True), False)
    assert WebullAdapter._position_intent(req) == "SELL_TO_CLOSE"


def test_an_opening_option_sell_still_goes_out_as_sell_to_open():
    req = broker_request_for(_Order(is_closing=False), False)
    assert WebullAdapter._position_intent(req) == "SELL_TO_OPEN"


def test_a_closing_option_buy_goes_out_as_buy_to_close():
    req = broker_request_for(_Order(is_closing=True, side=OrderSide.BUY), False)
    assert WebullAdapter._position_intent(req) == "BUY_TO_CLOSE"


def test_the_bracket_legs_are_dropped_when_the_broker_cannot_hold_them():
    o = _Order()
    o.take_profit_price = Decimal("5")
    o.stop_loss_price = Decimal("1")
    assert broker_request_for(o, False).take_profit_price is None
    assert broker_request_for(o, True).take_profit_price == Decimal("5")


# ── extended hours: only outside the regular session ─────────────────────────

class _ExtAdapter:
    """An adapter that needs the flag to trade pre/post-market (Alpaca, Webull)."""
    requires_extended_hours_limit = True


class _PlainAdapter:
    """One that routes extended hours itself (SnapTrade)."""
    requires_extended_hours_limit = False


def _ext(monkeypatch, *, in_extended: bool, adapter=None):
    from app.services import copy_engine, market_hours
    monkeypatch.setattr(market_hours, "in_extended_hours", lambda *a, **k: in_extended)
    return copy_engine.needs_extended_hours_limit(adapter or _ExtAdapter())


def test_the_flag_is_off_during_the_regular_session(monkeypatch):
    """The guarantee that matters: ordinary market-hours trading and copying
    behave exactly as before, because the rule simply does not fire."""
    assert _ext(monkeypatch, in_extended=False) is False


def test_the_flag_is_on_in_pre_and_post_market(monkeypatch):
    assert _ext(monkeypatch, in_extended=True) is True


def test_a_broker_that_routes_itself_never_gets_the_flag(monkeypatch):
    """SnapTrade trades extended hours natively; flagging it would only make
    the order miss."""
    assert _ext(monkeypatch, in_extended=True, adapter=_PlainAdapter()) is False


def test_a_regular_hours_order_carries_no_flag():
    assert broker_request_for(_Order(), False).extended_hours is False


def test_an_extended_hours_order_carries_the_flag():
    assert broker_request_for(_Order(), False, extended_hours=True).extended_hours is True


def test_the_trader_path_uses_the_same_rule_as_the_copy_path():
    """The two drifting is what produced a subscriber's mirror filling
    pre-market while the trader they copy sat unfilled at the same price."""
    import inspect

    from app.api.trades import _place_trader_order

    src = inspect.getsource(_place_trader_order)
    assert "needs_extended_hours_limit(adapter)" in src


# ── options never trade extended hours ───────────────────────────────────────

def test_an_option_order_never_claims_extended_hours():
    """US options do not trade outside the regular session, so the request must
    not say they might. The copy path gates on instrument_type == STOCK; the
    trader path has to as well or the two disagree."""
    import inspect

    from app.api.trades import _place_trader_order

    src = inspect.getsource(_place_trader_order)
    gate = src[src.index("extended_hours=("):]
    assert "InstrumentType.OPTION" in gate[:300]


def test_the_alpaca_adapter_refuses_to_send_it_on_an_option():
    """Enforced at the adapter too: it is a fact about the market, not a policy
    a caller should be able to override by passing the flag anyway."""
    from decimal import Decimal as D

    from app.brokers.alpaca import AlpacaAdapter
    from app.brokers.base import BrokerOrderRequest
    from app.models.order import InstrumentType as IT, OptionRight as OR, OrderSide, OrderType

    sent = {}

    class _Client:
        def submit_order(self, order_data=None, **kw):
            sent["req"] = order_data
            raise RuntimeError("stop before the network")

    a = AlpacaAdapter.__new__(AlpacaAdapter)
    a._c = lambda: _Client()

    req = BrokerOrderRequest(
        instrument_type=IT.OPTION, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=D(1), limit_price=D("2.50"),
        option_expiry=date(2026, 12, 18), option_strike=D("250"),
        option_right=OR.CALL, extended_hours=True,      # caller insists
    )
    try:
        a.place_order(req)
    except RuntimeError:
        pass
    assert getattr(sent["req"], "extended_hours", None) is not True


def test_a_stock_order_still_sends_it():
    """The guard must not have disabled the fix it was protecting."""
    from decimal import Decimal as D

    from app.brokers.alpaca import AlpacaAdapter
    from app.brokers.base import BrokerOrderRequest
    from app.models.order import InstrumentType as IT, OrderSide, OrderType

    sent = {}

    class _Client:
        def submit_order(self, order_data=None, **kw):
            sent["req"] = order_data
            raise RuntimeError("stop before the network")

    a = AlpacaAdapter.__new__(AlpacaAdapter)
    a._c = lambda: _Client()

    req = BrokerOrderRequest(
        instrument_type=IT.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=D(1), limit_price=D("338"),
        extended_hours=True,
    )
    try:
        a.place_order(req)
    except RuntimeError:
        pass
    assert sent["req"].extended_hours is True
