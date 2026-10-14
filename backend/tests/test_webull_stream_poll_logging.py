"""The per-order log line names its source — STREAM push vs REST poll.

WHY THIS EXISTS
---------------
The listener logged one line per STREAM frame but stayed silent on
poll-detected orders. A placement the stream never delivers (Webull does not
push the initial new-order ack; the poller is what surfaces it) therefore left
no trace in the container log, so the stream looked dead whenever someone
placed an order and watched the logs — even though fills, cancels and replaces
were streaming fine. This pins that BOTH sources are logged, so the live log
agrees with the durable detected_via field (see test_detected_via.py).

Pure logic: no SDK, no DB, no network. Shadow mode (the default) makes
_on_order_event a log-only path, and the source line fires before any DB work.
"""
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.webull_listener as wl

_PAYLOAD = {
    "symbol": "SPY", "side": "BUY", "order_status": "FILLED",
    "category": "US_OPTION", "order_id": "WB123ABC", "filled_qty": "2.00",
    "filled_price": "1.31", "filled_time": "2026-10-14T15:13:50.000+0000",
}


def test_stream_frame_logs_a_STREAM_line(caplog):
    """A frame carrying eventType (the gRPC push) is tagged STREAM."""
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    with caplog.at_level(logging.INFO, logger=wl.log.name):
        wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD,
                           event_type=1024, subscribe_type=1)
    assert any("STREAM SPY BUY FILLED" in r.getMessage() for r in caplog.records), \
        "a stream frame must log a STREAM line"


def test_poll_frame_logs_a_POLL_line(caplog):
    """The poller calls positionally (event_type=None); it must still log, as
    POLL — the line whose absence made the stream look dead."""
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    with caplog.at_level(logging.INFO, logger=wl.log.name):
        wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD,
                           event_type=None, subscribe_type=None)
    assert any("POLL SPY BUY FILLED" in r.getMessage() for r in caplog.records), \
        "a poll frame must log a POLL line, not be silent"


def test_source_line_is_not_gated_to_stream_only():
    """Pin the fix in source: the old code logged only when _via == 'stream'."""
    import inspect
    src = inspect.getsource(wl._on_order_event)
    assert "_via.upper()" in src, "the log must tag the source for both paths"
    assert 'if _via == "stream" and isinstance(payload, dict):' not in src, \
        "the stream-only gate must be gone so poll frames are logged too"


if __name__ == "__main__":
    print("run under pytest")
