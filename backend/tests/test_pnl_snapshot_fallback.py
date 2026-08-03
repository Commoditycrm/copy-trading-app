"""Unit tests for pnl_poller's last-known-snapshot fallback.

Regression for the prod report: a subscriber's Daily Profit Limit never
stopped copy trading because every tick's broker balance fetch failed, and
the poller bailed BEFORE the kill switch ran. The fix caches the last good
snapshot and enforces the daily limits against it (marked ``degraded``)
while it's fresh — but auto-liquidation stays gated to fresh data only.

Pure-logic tests — no DB or broker needed.

Run standalone:  .venv/bin/python tests/test_pnl_snapshot_fallback.py
Or under pytest: pytest tests/test_pnl_snapshot_fallback.py
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.pnl_poller as pp

_T0 = datetime(2026, 8, 3, 15, 0, 0, tzinfo=timezone.utc)
_SNAP = {"todays_pl": 600, "equity": 10_600, "beginning_day_balance": 10_000}


def _reset():
    pp._LAST_SNAPSHOT.clear()


def test_live_success_caches_and_is_not_degraded():
    _reset()
    acct = uuid.uuid4()
    state, degraded = pp._snapshot_or_last_known(acct, _SNAP, _T0)
    assert state == _SNAP
    assert degraded is False
    assert acct in pp._LAST_SNAPSHOT


def test_failure_falls_back_to_fresh_cache_as_degraded():
    """The core fix: a failed live fetch still enforces on the last snapshot."""
    _reset()
    acct = uuid.uuid4()
    pp._snapshot_or_last_known(acct, _SNAP, _T0)              # seed a good one
    later = _T0 + timedelta(seconds=120)                     # 2 min later, fetch fails
    state, degraded = pp._snapshot_or_last_known(acct, None, later)
    assert state == _SNAP, "should enforce against the last-known snapshot"
    assert degraded is True, "must be flagged degraded (so liquidation is skipped)"


def test_failure_with_stale_cache_skips():
    """Too old → don't auto-pause on unreliable data; skip like before."""
    _reset()
    acct = uuid.uuid4()
    pp._snapshot_or_last_known(acct, _SNAP, _T0)
    later = _T0 + timedelta(seconds=pp._SNAPSHOT_STALE_LIMIT_S + 1)
    state, degraded = pp._snapshot_or_last_known(acct, None, later)
    assert state is None
    assert degraded is False


def test_failure_with_no_cache_skips():
    _reset()
    acct = uuid.uuid4()
    state, degraded = pp._snapshot_or_last_known(acct, None, _T0)
    assert state is None
    assert degraded is False


def test_boundary_exactly_at_limit_is_still_used():
    _reset()
    acct = uuid.uuid4()
    pp._snapshot_or_last_known(acct, _SNAP, _T0)
    later = _T0 + timedelta(seconds=pp._SNAPSHOT_STALE_LIMIT_S)  # exactly at limit
    state, degraded = pp._snapshot_or_last_known(acct, None, later)
    assert state == _SNAP
    assert degraded is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll snapshot-fallback tests passed.")
