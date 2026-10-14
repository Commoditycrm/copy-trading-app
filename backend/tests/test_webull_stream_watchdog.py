"""A connected-but-silent Webull stream is detected and recycled.

WHY THIS EXISTS
---------------
On QA, 2026-10-14, the gRPC event stream delivered order frames until 11:10 ET
and then went quiet while still "connected". do_subscribe only returns on a
drop or error, so nothing recycled it, and the listener never bumped
last_event_at, so the "Broker live" pill could not tell. Every order then came
through the REST poller ~10s late. These tests pin the watchdog: stream frames
count as liveness, poll-only fills/cancels count as misses, enough misses mark
the stream silent and force ONE resubscribe, and resubscribes are rate-limited.

Pure logic: no SDK, no DB, no Redis. Shadow mode (the default) keeps
_on_order_event log-only, and listener_state's Redis/SSE side effects are
stubbed out.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.webull_listener as wl
from app.services import listener_state


class _FakeClient:
    def __init__(self):
        self.stops = 0

    def request_stop(self):
        self.stops += 1


@pytest.fixture
def states():
    """Every listener_state.set_state call, as (state, error)."""
    return []


@pytest.fixture
def tid(monkeypatch, states):
    t = uuid.uuid4()
    monkeypatch.setattr(listener_state, "_mirror_to_redis", lambda *a, **k: None)
    monkeypatch.setattr(listener_state, "_broadcast_state_changed", lambda *a, **k: None)
    real_set = listener_state.set_state

    def _record(trader_user_id, state, *, error=None):
        states.append((state, error))
        real_set(trader_user_id, state, error=error)

    monkeypatch.setattr(listener_state, "set_state", _record)
    wl._generation[t] = 1
    yield t
    wl._watchdog_clear(t)
    wl._clients.pop(t, None)
    wl._generation.pop(t, None)
    listener_state._status.pop(t, None)


def _fill(t, oid, now):
    """The poller sees a resting order fill."""
    wl._watchdog_poll_change(t, oid, "WORKING", "FILLED", now=now)


def _misses(t, n, start):
    """n poll-only fills at ``start``, then a tick past the grace period."""
    for i in range(n):
        _fill(t, f"{start}-{i}", start)
    wl._watchdog_tick(t, now=start + wl._WD_GRACE_S + 1)


_PAYLOAD = {
    "symbol": "SPY", "side": "BUY", "order_status": "FILLED",
    "category": "US_OPTION", "order_id": "WB1", "filled_qty": "1",
    "filled_price": "1.31",
}


def test_stream_order_frame_bumps_last_event_at(tid):
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD, event_type=1024, subscribe_type=7)
    s = listener_state._status.get(tid)
    assert s is not None and s.last_event_at is not None


def test_poll_frame_does_not_bump_last_event_at(tid):
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD)  # poller: no eventType
    s = listener_state._status.get(tid)
    assert s is None or s.last_event_at is None


def test_non_order_stream_frame_does_not_bump(tid):
    """Position / PnL frames carry no order_id — not proof the ORDER feed works."""
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, {"symbol": "SPY"}, event_type=1, subscribe_type=2)
    s = listener_state._status.get(tid)
    assert s is None or s.last_event_at is None


def test_poll_only_changes_past_threshold_trigger_exactly_one_resubscribe(tid, states):
    client = wl._clients[tid] = _FakeClient()
    for i in range(wl._WD_MISSES):
        _fill(tid, f"o{i}", now=0.0)
    wl._watchdog_tick(tid, now=5.0)          # still inside the stream's grace period
    assert client.stops == 0
    wl._watchdog_tick(tid, now=wl._WD_GRACE_S + 1)
    assert client.stops == 1
    assert states[-1][0] == wl.STREAM_SILENT
    assert "polling" in (states[-1][1] or "")
    # More ticks, and more misses inside the rate-limit gap, do not stop it again.
    wl._watchdog_tick(tid, now=20.0)
    _misses(tid, wl._WD_MISSES, start=30.0)
    assert client.stops == 1


def test_below_threshold_does_nothing(tid, states):
    client = wl._clients[tid] = _FakeClient()
    _misses(tid, wl._WD_MISSES - 1, start=0.0)
    assert client.stops == 0
    assert all(s != wl.STREAM_SILENT for s, _ in states)


def test_stream_frame_resets_the_count(tid):
    client = wl._clients[tid] = _FakeClient()
    _misses(tid, wl._WD_MISSES - 1, start=0.0)
    wl._watchdog_stream_frame(tid, "other", now=20.0)
    _misses(tid, wl._WD_MISSES - 1, start=30.0)
    assert client.stops == 0


def test_stream_delivering_the_order_within_grace_is_not_a_miss(tid):
    client = wl._clients[tid] = _FakeClient()
    for i in range(wl._WD_MISSES):
        _fill(tid, f"o{i}", now=0.0)
    wl._watchdog_stream_frame(tid, "o0", now=3.0)   # stream caught up
    wl._watchdog_tick(tid, now=wl._WD_GRACE_S + 1)
    assert client.stops == 0


def test_stream_delivered_before_poll_is_not_a_miss(tid):
    client = wl._clients[tid] = _FakeClient()
    for i in range(wl._WD_MISSES):
        wl._watchdog_stream_frame(tid, f"o{i}", now=0.0)
        _fill(tid, f"o{i}", now=2.0)
    wl._watchdog_tick(tid, now=60.0)
    assert client.stops == 0


def test_new_resting_order_is_not_a_miss(tid):
    """Webull does not push the new-order ack; only the poller sees it."""
    client = wl._clients[tid] = _FakeClient()
    for i in range(wl._WD_MISSES + 2):
        wl._watchdog_poll_change(tid, f"o{i}", None, "WORKING", now=0.0)
        wl._watchdog_poll_change(tid, f"m{i}", "WORKING", "WORKING", now=0.0)  # a modify
    wl._watchdog_tick(tid, now=60.0)
    assert client.stops == 0


def test_resubscribes_are_rate_limited(tid, states):
    client = wl._clients[tid] = _FakeClient()
    _misses(tid, wl._WD_MISSES, start=0.0)
    assert client.stops == 1
    _misses(tid, wl._WD_MISSES, start=60.0)
    assert client.stops == 1, "a second resubscribe inside the gap"
    assert states[-1][0] == wl.STREAM_SILENT, "still reported silent while rate-limited"
    # Past the gap, the misses already counted are enough to try again.
    first = wl._WD_GRACE_S + 1   # when the first resubscribe happened
    wl._watchdog_tick(tid, now=first + wl._WD_RESUBSCRIBE_GAP_S - 1)
    assert client.stops == 1
    wl._watchdog_tick(tid, now=first + wl._WD_RESUBSCRIBE_GAP_S)
    assert client.stops == 2


def test_long_quiet_window_trips_on_fewer_misses(tid):
    client = wl._clients[tid] = _FakeClient()
    wl._watchdog_stream_frame(tid, "old", now=0.0)
    _misses(tid, wl._WD_WINDOW_MISSES, start=wl._WD_WINDOW_S + 5)
    assert client.stops == 1


def test_no_live_client_leaves_state_to_the_listener(tid, states):
    """Mid-reconnect (no client) the watchdog must not overwrite e.g.
    in_use_elsewhere with 'silent'."""
    _misses(tid, wl._WD_MISSES, start=0.0)
    assert all(s != wl.STREAM_SILENT for s, _ in states)


def test_silent_is_sticky_across_resubscribe_until_a_stream_frame(tid, states):
    wl._clients[tid] = _FakeClient()
    _misses(tid, wl._WD_MISSES, start=0.0)
    wl._set_state(tid, "connected")          # what _run_listener says on resubscribe
    assert states[-1][0] == wl.STREAM_SILENT
    wl._watchdog_stream_frame(tid, "o", now=50.0)
    assert states[-1] == ("connected", None)


def test_stop_listener_clears_the_watchdog(tid, monkeypatch, states):
    wl._clients[tid] = _FakeClient()
    _misses(tid, wl._WD_MISSES, start=0.0)
    wl.stop_listener(tid)
    assert tid not in wl._watchdogs and tid not in wl._stream_silent
    wl._set_state(tid, "connected")
    assert states[-1][0] == "connected"


def test_watchdog_stop_reconnects_quietly(monkeypatch, caplog):
    """Closing the channel ourselves makes the SDK raise "Channel closed!". That
    is the resubscribe working, not a failure: no ERROR traceback, and no raw
    gRPC text shown as last_error (seen on QA, 2026-10-14 13:04 ET)."""
    import asyncio
    import logging
    import threading

    t, aid = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(wl, "_load_creds", lambda _aid: {"app_key": "K", "account_id": "A1"})

    class _Client:
        def __init__(self):
            self._stop_event = threading.Event()

        def do_subscribe(self, ids):
            self._stop_event.set()   # what request_stop() does
            raise RuntimeError('status = StatusCode.CANCELLED\n\tdetails = "Channel closed!"')

    monkeypatch.setattr(wl, "_build_stoppable_client", lambda creds: _Client())
    monkeypatch.setattr(wl, "_all_account_ids", lambda creds: ["A1"])
    monkeypatch.setattr(listener_state, "_mirror_to_redis", lambda *a, **k: None)
    monkeypatch.setattr(listener_state, "_broadcast_state_changed", lambda *a, **k: None)
    states = []
    real_set = listener_state.set_state
    monkeypatch.setattr(listener_state, "set_state",
                        lambda tid, s, *, error=None: (states.append((s, error)),
                                                       real_set(tid, s, error=error)))

    async def _sleep(_s):
        raise asyncio.CancelledError   # stop after the first reconnect

    monkeypatch.setattr(wl.asyncio, "sleep", _sleep)
    with caplog.at_level(logging.INFO, logger=wl.log.name):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(wl._run_listener(t, aid))
    listener_state._status.pop(t, None)

    assert ("reconnecting", None) in states
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
