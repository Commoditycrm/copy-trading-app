"""A rejection notice must name the CONTRACT, not just the ticker.

"Your SELL SPY order was rejected" is unactionable when several SPY contracts
are open — the reader cannot tell which one the broker refused. These pin the
descriptor at the shape the Positions and Trades tables use, so the notice and
the row a user goes looking for read the same.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.models.order import InstrumentType, OptionRight
from app.services.notifications import instrument_label


def _order(**kw):
    base = dict(symbol="spy", instrument_type=InstrumentType.OPTION,
                option_right=OptionRight.CALL, option_strike=Decimal("770"),
                option_expiry=date(2026, 9, 25))
    base.update(kw)
    return NS(**base)


def test_an_option_is_named_in_full():
    assert instrument_label(_order()) == "SPY C $770 25 Sep 26"


def test_a_put_reads_p():
    assert instrument_label(_order(option_right=OptionRight.PUT)) == "SPY P $770 25 Sep 26"


def test_a_stock_is_just_the_ticker():
    assert instrument_label(_order(instrument_type=InstrumentType.STOCK)) == "SPY"


@pytest.mark.parametrize("strike,shown", [
    (Decimal("770.00"), "$770"),   # a whole strike must not come out as 7.7E+2
    (Decimal("4.50"), "$4.5"),     # trailing zero dropped, like the UI
    (Decimal("612.5"), "$612.5"),
    ("612.5", "$612.5"),           # broker payloads hand us strings
    (4.5, "$4.5"),                 # ...and floats
])
def test_the_strike_renders_like_the_positions_table(strike, shown):
    assert shown in instrument_label(_order(option_strike=strike))


@pytest.mark.parametrize("missing", ["option_expiry", "option_strike", "option_right"])
def test_a_missing_leg_degrades_instead_of_raising(missing):
    # A half-populated option row still has to produce a readable notice:
    # the notification path must never be what raises.
    label = instrument_label(_order(**{missing: None}))
    assert label.startswith("SPY") and "None" not in label


def test_a_symbolless_order_does_not_say_none():
    assert instrument_label(_order(symbol=None, instrument_type=InstrumentType.STOCK)) == "—"
