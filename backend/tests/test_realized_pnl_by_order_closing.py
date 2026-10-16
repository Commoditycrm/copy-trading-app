"""realized_pnl_by_order: a CLOSE never opens a position.

Seen on the Positions page's "Closed today" table: every exit on a contract
showed no realized P&L. One close had no entry in the P&L history (its buy was
hidden), so the walk booked that sell as a new SHORT. The next buy then
"covered" it — taking the P&L — and its own exits opened shorts in turn, so on
that contract the entries carried the P&L and the exits carried none.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from app.models.order import InstrumentType, OptionRight, OrderSide
from app.services.pnl import realized_pnl_by_order

T0 = datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _DB:
    """First query = the orders, second = their fills (none: order-level times)."""

    def __init__(self, orders):
        self._answers = [orders, []]

    def execute(self, _stmt):
        return _Result(self._answers.pop(0))


def _order(minute, side, qty, price, closing=False):
    when = T0 + timedelta(minutes=minute)
    return SimpleNamespace(
        id=uuid.uuid4(), instrument_type=InstrumentType.OPTION, symbol="SPY",
        option_expiry=date(2026, 9, 29), option_strike=D("770"), option_right=OptionRight.PUT,
        side=side, filled_quantity=D(qty), filled_avg_price=D(price), is_closing=closing,
        closed_at=when, submitted_at=when, created_at=when,
    )


def _pnl(orders):
    return realized_pnl_by_order(_DB(orders), uuid.uuid4())


def test_close_without_a_known_entry_does_not_flip_the_book():
    orphan = _order(0, OrderSide.SELL, 5, "6.30", closing=True)   # its buy is not in the history
    buy = _order(10, OrderSide.BUY, 10, "6.15")
    trim = _order(11, OrderSide.SELL, 5, "6.13", closing=True)
    rest = _order(12, OrderSide.SELL, 5, "6.35", closing=True)

    by = _pnl([orphan, buy, trim, rest])

    assert orphan.id not in by            # nothing to price it against
    assert buy.id not in by               # an entry realizes nothing
    assert by[trim.id] == D("-10.00")     # (6.13 - 6.15) x 5 x 100
    assert by[rest.id] == D("100.00")     # (6.35 - 6.15) x 5 x 100


def test_close_larger_than_the_known_position_realizes_only_what_is_known():
    buy = _order(0, OrderSide.BUY, 3, "1.00")
    close = _order(1, OrderSide.SELL, 5, "1.50", closing=True)
    again = _order(2, OrderSide.BUY, 2, "1.20")

    by = _pnl([buy, close, again])

    assert by[close.id] == D("150.00")    # 3 known contracts; the other 2 open no short
    assert again.id not in by


def test_sell_to_open_still_opens_a_short_and_the_buy_back_realizes():
    short = _order(0, OrderSide.SELL, 2, "2.00")                  # not a close: sold to open
    cover = _order(5, OrderSide.BUY, 2, "1.40", closing=True)

    by = _pnl([short, cover])

    assert short.id not in by
    assert by[cover.id] == D("120.00")    # (2.00 - 1.40) x 2 x 100


def test_ordinary_round_trips_are_unchanged():
    b1 = _order(0, OrderSide.BUY, 4, "1.02")
    s1 = _order(1, OrderSide.SELL, 2, "1.35", closing=True)
    s2 = _order(2, OrderSide.SELL, 2, "0.88")                     # a close made at the broker: not flagged
    by = _pnl([b1, s1, s2])
    assert by[s1.id] == D("66.00")
    assert by[s2.id] == D("-28.00")
