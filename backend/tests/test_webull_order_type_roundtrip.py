"""Webull's order-type names have to map back to the ones we sent.

WebullAdapter._ORDER_TYPE_MAP sends STOP as "STOP_LOSS". The listener did not
recognise that name -- while it DID recognise "STOP_LOSS_LIMIT" -- so a plain
stop came back as MARKET. The listener's modify branch then treated the type
as changed and rewrote our own row, so a resting protective STOP was stored,
and displayed, as a MARKET order.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.webull import WebullAdapter
from app.models.order import OrderType
from app.services.webull_listener import _map_order_type


def test_every_type_we_send_maps_back_to_itself():
    """The round trip is the whole contract. A name we SEND that we cannot READ
    silently rewrites the order to MARKET."""
    for ours, theirs in WebullAdapter._ORDER_TYPE_MAP.items():
        assert _map_order_type(theirs) == ours, f"{theirs} did not map back to {ours}"


def test_a_stop_is_not_read_as_a_market_order():
    assert _map_order_type("STOP_LOSS") == OrderType.STOP


def test_a_stop_limit_still_maps():
    assert _map_order_type("STOP_LOSS_LIMIT") == OrderType.STOP_LIMIT


def test_an_unknown_type_still_falls_back_to_market():
    assert _map_order_type("SOMETHING_NEW") == OrderType.MARKET
    assert _map_order_type(None) == OrderType.MARKET
