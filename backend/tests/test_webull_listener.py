"""Unit tests for the direct-Webull listener's safety guards.

No SDK, no DB, no network — pure logic:
  * feature flags default to the SAFE state (off / shadow),
  * the generation guard drops events from a superseded listener (so a
    lingering gRPC thread after a restart can never fan out), and
  * shadow mode is a pure log — it never touches the DB or fanout.

Run standalone:  .venv/bin/python tests/test_webull_listener.py
Or under pytest: pytest tests/test_webull_listener.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.webull_listener as wl
from app.config import get_settings
from app.models.order import OrderSide, OrderStatus

_PAYLOAD = {
    "symbol": "APP", "side": "BUY", "order_status": "FILLED",
    "category": "US_OPTION", "order_id": "D5G4DKBS", "filled_qty": "1.00",
    "filled_price": "4.50", "filled_time": "2026-08-06T14:04:46.424+0000",
}


def test_flags_default_safe():
    # Assert the CODE defaults are safe (a local .env may override at runtime,
    # so check the field defaults on the Settings class, not get_settings()).
    from app.config import Settings
    assert Settings.model_fields["webull_direct_enabled"].default is False, \
        "direct Webull must default OFF in code"
    assert Settings.model_fields["webull_direct_shadow_mode"].default is True, \
        "shadow mode must default ON in code"


def test_generation_guard_drops_stale_events():
    """An event tagged with an OLD generation (a superseded listener) is dropped
    before any shadow-log or fanout — the guard against double-mirroring after a
    listener restart."""
    tid = uuid.uuid4()
    wl._generation[tid] = 5
    # Stale generation 3 != current 5 → must return immediately, no raise.
    wl._on_order_event(tid, uuid.uuid4(), 3, {}, _PAYLOAD)   # should be a no-op


def test_current_generation_shadow_is_pure_log():
    """With the current generation and shadow mode (default ON), the handler
    logs and returns without touching the DB or fanout — no exception."""
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD)   # shadow path: log-only


def test_non_dict_payload_is_ignored():
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, "not-a-dict")   # must not raise


def test_rest_order_to_payload_stock():
    """A REST today-orders stock row flattens into the same payload shape the
    gRPC handler consumes (leg detail from items[0], ids from the wrapper)."""
    row = {
        "items": [{"symbol": "NIO", "category": "US_STOCK", "filled_price": "4.5600",
                   "filled_qty": "1", "last_filled_time": "2026-08-06 16:47:17.816+0000",
                   "order_status": "FILLED", "order_type": "LIMIT", "qty": "1",
                   "side": "SELL", "limit_price": "4.560"}],
        "client_order_id": "coid", "order_id": "OID123",
        "account_id": "ACC", "order_type": "LMT",
    }
    p = wl._rest_order_to_payload(row)
    assert p["order_id"] == "OID123"
    assert p["symbol"] == "NIO" and p["side"] == "SELL"
    assert p["category"] == "US_STOCK"
    assert p["order_status"] == "FILLED"
    # space separator normalised to 'T' so the ISO parser accepts it
    assert p["filled_time"] == "2026-08-06T16:47:17.816+0000"
    assert wl._map_status(p["order_status"]) == OrderStatus.FILLED
    assert wl._map_side(p["side"]) == OrderSide.SELL


def test_rest_order_to_payload_option_and_no_id():
    opt = {"items": [{"symbol": "OPRA", "category": "US_OPTION", "order_status": "FILLED",
                      "side": "SELL", "qty": "1", "order_type": "MARKET"}],
           "order_id": "OPT1", "client_order_id": "c", "account_id": "A"}
    p = wl._rest_order_to_payload(opt)
    assert p["category"].upper() == "US_OPTION"
    # a row without an order_id is dropped (can't dedup/persist it)
    assert wl._rest_order_to_payload({"items": [{"symbol": "X"}]}) is None


def test_parse_wb_time_both_formats():
    """Both Webull timestamp forms parse to an aware UTC datetime."""
    a = wl._parse_wb_time("2026-08-06T14:04:46.424+0000")   # stream (ISO 'T')
    b = wl._parse_wb_time("2026-08-06 16:47:17.816+0000")   # REST (space)
    assert a is not None and a.utcoffset().total_seconds() == 0
    assert b is not None and b.hour == 16 and b.minute == 47
    assert wl._parse_wb_time(None) is None
    assert wl._parse_wb_time("garbage") is None


def test_safe_poll_interval_respects_rate_limit():
    """The effective interval never lets list_today_orders exceed Webull's
    10-req/30s app-id cap: it floors at 3.5s and scales by account count."""
    # single account: at least the 3.5s floor
    assert wl._safe_poll_interval(1) >= 3.5
    # more accounts ⇒ longer cycle (≥ ~3.3s per account)
    assert wl._safe_poll_interval(3) >= 3.3 * 3
    # a cycle at the returned interval stays within 10 calls / 30s
    for n in (1, 2, 3, 5):
        calls_per_30s = n * (30.0 / wl._safe_poll_interval(n))
        assert calls_per_30s <= 10.0 + 1e-9, (n, calls_per_30s)


def test_order_fingerprint_catches_modify():
    """The poll fingerprint changes on a MODIFY (price/qty edit) even when the
    status is unchanged — otherwise the poller would skip modifications."""
    base = {"order_status": "PENDING", "order_type": "LIMIT", "qty": "1",
            "limit_price": "3.80", "stop_price": None, "filled_qty": "0",
            "filled_price": None}
    fp0 = wl._order_fingerprint(base)
    # same order, re-seen unchanged → same fingerprint (poller skips)
    assert wl._order_fingerprint(dict(base)) == fp0
    # price modified, status still PENDING → fingerprint MUST differ
    assert wl._order_fingerprint({**base, "limit_price": "3.95"}) != fp0
    # qty modified → differs
    assert wl._order_fingerprint({**base, "qty": "2"}) != fp0
    # incremental fill within a working order → differs
    assert wl._order_fingerprint({**base, "filled_qty": "1"}) != fp0


def test_public_interface_matches_other_listeners():
    for name in ("bind_loop", "start_all_listeners", "start_listener", "stop_listener",
                 "stop_all_listeners", "has_running_listener", "running_trader_ids"):
        assert hasattr(wl, name), f"missing public function {name}"
    assert isinstance(wl._tasks, dict)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull-listener guard tests passed.")
