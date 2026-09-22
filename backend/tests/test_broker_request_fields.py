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
