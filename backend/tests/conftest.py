"""Shared pytest setup.

The SnapTrade fill nudge (services/snaptrade_nudge) spawns a background worker
thread that makes real SnapTrade calls and opens its own DB sessions. It is
triggered from copy_engine's placement paths, so it would fire during any test
that exercises a fanout. Off by default here; tests that want it re-enable it
explicitly (see test_snaptrade_nudge.py).
"""
import pytest

from app.services import snaptrade_nudge


@pytest.fixture(autouse=True)
def _disable_snaptrade_nudge():
    snaptrade_nudge.set_enabled(False)
    yield
    snaptrade_nudge.set_enabled(False)


@pytest.fixture(autouse=True)
def _no_live_alpaca_quotes(monkeypatch):
    """Stops and trailing exits are judged on Alpaca's live quote
    (services/live_marks), read from the shared price cache or the data API.
    A test must not pick up whatever quote a developer's local Redis happens to
    hold, or call Alpaca: no quote, so decisions use the position's own mark —
    the price each test sets. Tests of live_marks itself replace these."""
    from app.services import market_data_stream

    monkeypatch.setattr(market_data_stream, "get_live_price", lambda *a, **k: None)
    monkeypatch.setattr(market_data_stream, "fetch_rest_quote", lambda *a, **k: None)
    yield
