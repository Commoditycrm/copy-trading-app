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
