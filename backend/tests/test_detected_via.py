"""Which path detected an order — stream push, or REST poll.

WHY THIS EXISTS
---------------
"Is the Webull realtime stream actually doing the work?" was unanswerable from
the data for two months. Both paths write through the same code and left no
trace, so a stream that was connected but discarding every event looked exactly
like one carrying the whole load. Every other column — status, filled quantity,
price, timestamps — is identical either way.

That ambiguity cost real time: a frame from the REST poller was read as proof
the stream had come back, and a verification query compared the stream's
order_id against the client_order_id we store, which can never match by
construction and so "measured" nothing.

These pin the one field that distinguishes them, and the broker fill time the
stream path used to drop.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def test_dispatch_tags_the_source():
    """_on_order_event knows which path it is on; _persist_and_fanout does not
    unless it is told. The stream passes eventType, the poller calls
    positionally — that difference is the ONLY signal available."""
    import inspect
    from app.services import webull_listener as wl
    src = inspect.getsource(wl._on_order_event)
    assert '_via = "poll" if event_type is None else "stream"' in src
    assert "via=_via" in src, "the source must reach _persist_and_fanout"


def test_persist_stamps_via_and_the_broker_fill_time():
    import inspect
    from app.services import webull_listener as wl
    src = inspect.getsource(wl._persist_and_fanout)
    assert "existing.detected_via = via" in src, "updates must record the source"
    assert "detected_via=via" in src, "inserts must record the source"
    # The stream carries filled_time; dropping it made trader latency
    # unmeasurable, because closed_at is OUR clock and not the broker's.
    assert "broker_filled_at" in src


def test_poll_is_the_default_so_an_untagged_caller_never_claims_stream():
    """A caller that forgets to pass `via` must not have its orders reported as
    realtime — that would overstate the stream's coverage, which is the exact
    error this column exists to prevent."""
    import inspect
    from app.services import webull_listener as wl
    sig = inspect.signature(wl._persist_and_fanout)
    assert sig.parameters["via"].default == "poll"


def test_the_column_is_exposed_to_the_ui():
    """The badge in Order History reads this field; without it on the schema the
    API silently drops it and the badge never renders."""
    from app.schemas.order import OrderOut
    assert "detected_via" in OrderOut.model_fields


def test_order_model_has_the_column():
    from app.models.order import Order
    assert hasattr(Order, "detected_via")


if __name__ == "__main__":
    print("run under pytest")
