"""A protective close must never be swallowed by duplicate suppression.

_place_trader_order drops an order that matches another within 3 seconds on
(symbol, side, type, qty, prices, option terms) and RETURNS THE EXISTING ONE --
placing nothing, and telling the caller nothing.

The poller's protective exit is shape-identical to the trim that usually
precedes it by a second or two: same contract, SELL, MARKET, same 1 contract.
So it was suppressed, and because the call appeared to succeed the ladder
retired the guard believing the position was closed -- leaving it open and no
longer tracked by anything. Live on 2026-09-23: trim 00:33:49, stop refused
00:33:51, close suppressed 00:33:52, guard retired "position closed".

Worse than not closing: the position is open AND unwatched.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.trades import _place_trader_order
from app.services.pnl_poller import _enforce_discord_trailing_stops


def test_the_placement_path_can_be_told_to_skip_dedup():
    params = inspect.signature(_place_trader_order).parameters
    assert "skip_dedup" in params, "no way to opt out of duplicate suppression"
    assert params["skip_dedup"].default is False, "dedup must stay on by default"


def test_the_dedup_lookup_is_actually_gated_on_the_flag():
    """The flag has to skip the LOOKUP, not just the branch -- otherwise the
    query still finds the trim and the order is still dropped."""
    src = inspect.getsource(_place_trader_order)
    assert "None if skip_dedup else db.execute(" in src


def test_the_protective_close_opts_out():
    """The one caller that cannot tolerate being silently skipped."""
    src = inspect.getsource(_enforce_discord_trailing_stops)
    close_body = src[src.index("def _close("):]
    assert "skip_dedup=True" in close_body, (
        "the poller's protective exit must bypass duplicate suppression"
    )
    # and it must still go out as a close, or the option SELL is SELL_TO_OPEN
    assert "resolve_wash_trade=True" in close_body
