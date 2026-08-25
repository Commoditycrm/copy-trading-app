"""Trailing-stop close support for the Sell-All flow.

The Sell-All variation is: instead of closing a position at market, close it with
a TRAILING STOP so it keeps riding a favourable move and only exits when the price
reverses by the trail. Not every broker offers trailing stops, and those that do
usually offer them on stocks only (Alpaca's options API rejects them) — so the
close flow asks this module, per position, whether a trailing stop is possible.
When it isn't, the caller falls back to its normal market/limit close.

"If and when the broker supports it" lives here:
  - the broker must advertise ``supports_trailing_stop`` (see BrokerAdapter), and
  - the position must be an eligible instrument (stocks today).
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter, BrokerPosition
from app.models.order import InstrumentType


def trailing_stop_supported(adapter: BrokerAdapter, position: BrokerPosition) -> bool:
    """True when this position can be closed with a native trailing stop on this
    broker. Stocks only for now — brokers that offer trailing stops (Alpaca) do
    not offer them on options."""
    if not getattr(adapter, "supports_trailing_stop", False):
        return False
    return position.instrument_type == InstrumentType.STOCK
