"""The simulated-prices screen must not spend the broker's request quota.

Webull allows roughly 10 requests per 30s across EVERY endpoint one key
touches, and answers simultaneous position reads with 429 outright. This
screen refreshes while the P&L poller and the positions page are reading the
same account, so an uncached read here surfaces as

    Couldn't read positions: HTTP 429 TOO_MANY_REQUESTS

on a page that only ever DISPLAYS.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import discord_sources
from app.brokers.webull import WebullAdapter


def test_the_screen_takes_the_shared_cached_read():
    src = inspect.getsource(discord_sources.list_simulated_prices)
    assert "get_positions(cached_ok=True)" in src, (
        "a display-only screen must share the cached positions read"
    )


def test_a_cached_read_is_served_without_a_second_broker_call(monkeypatch):
    """The flag has to actually collapse calls, not just be accepted."""
    import app.brokers.webull as wb

    a = WebullAdapter.__new__(WebullAdapter)
    a.app_key = "k"
    a.account_id = "acct-1"
    calls: list[int] = []
    monkeypatch.setattr(
        WebullAdapter, "_fetch_positions", lambda self: calls.append(1) or []
    )
    wb._positions_cache.clear()

    a.get_positions(cached_ok=True)
    a.get_positions(cached_ok=True)
    assert len(calls) == 1, "the second display read should reuse the first"

    # A decision path must still read live -- handing auto_liquidator a stale
    # snapshot risks closing a position that is already closed.
    a.get_positions()
    assert len(calls) == 2
