"""Webull reads take turns instead of being refused (services/webull_pacer.py).

QA 2026-10-20: 30 calls an hour came back 429 — 22 from the P&L poller, 8 on a
close or an alert order the trader was waiting on. Webull counts each endpoint
on its own (2 per 2s for positions, balance and the order queries) and refuses
calls that land close together; six parts of the app read positions on their
own clocks, in two processes.
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import webull_pacer as pacer
from app.services import webull_usage as wu


class _Redis:
    """The reservation script, done in Python: max(now, last + gap)."""

    def __init__(self):
        self.last = {}
        self.h = {}

    def eval(self, _script, _n, key, gap, max_wait, now):
        slot = max(now, self.last.get(key, 0) + gap)
        if slot - now > max_wait:
            return -1
        self.last[key] = slot
        return slot

    def hincrby(self, k, f, n):
        self.h.setdefault(k, {})
        self.h[k][f] = self.h[k].get(f, 0) + n

    def expire(self, k, s):
        pass

    def hgetall(self, k):
        return dict(self.h.get(k, {}))


@pytest.fixture
def r(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(pacer, "_redis", lambda: fake)
    monkeypatch.setattr(wu, "_redis", lambda: fake)
    return fake


def _run(calls, at=1000.0):
    """Each call at the same instant; returns the waits."""
    waits = []
    for action in calls:
        waits.append(pacer.wait_turn("key", action, sleep=lambda s: None, clock=lambda: at))
    return waits


def test_reads_of_one_endpoint_at_the_same_instant_are_spaced(r):
    w = _run(["/openapi/assets/positions"] * 3)
    assert w == [0.0, pacer.GAP_S, 2 * pacer.GAP_S]


def test_positions_balance_and_orders_each_have_their_own_turn(r):
    """A balance read does not wait on positions, nor an order query on either."""
    w = _run(["/openapi/assets/positions", "/openapi/assets/balance", "/trade/orders/list-today"])
    assert w == [0.0, 0.0, 0.0]


def test_the_order_queries_take_turns_as_one(r):
    """QA 2026-10-20: order-detail reads paced only against each other were
    still refused beside the order poll's list of today's orders."""
    w = _run(["/trade/orders/list-today", "/openapi/trade/order/detail"])
    assert w == [0.0, pacer.GAP_S]


def test_three_calls_span_well_over_two_seconds():
    """2 per 2s, judged where the call LANDS: the third must leave room for it
    to arrive late."""
    assert 2 * pacer.GAP_S >= 2.4


def test_orders_are_never_paced(r):
    """Place / replace / cancel are on a 600-per-minute counter."""
    w = _run(["/openapi/trade/option/order/place", "/openapi/trade/option/order/place",
              "/openapi/trade/option/order/cancel", "/openapi/trade/option/order/replace"])
    assert w == [0.0, 0.0, 0.0, 0.0]


def test_a_call_after_the_gap_does_not_wait(r):
    pacer.wait_turn("key", "/openapi/assets/positions", sleep=lambda s: None, clock=lambda: 1000.0)
    w = pacer.wait_turn("key", "/openapi/assets/positions", sleep=lambda s: None,
                        clock=lambda: 1000.0 + pacer.GAP_S)
    assert w == 0.0


def test_app_keys_do_not_wait_on_each_other(r):
    pacer.wait_turn("a", "/openapi/assets/positions", sleep=lambda s: None, clock=lambda: 1000.0)
    assert pacer.wait_turn("b", "/openapi/assets/positions", sleep=lambda s: None, clock=lambda: 1000.0) == 0.0


def test_a_queue_longer_than_the_cap_sends_at_once_and_reserves_nothing(r):
    w = _run(["/openapi/assets/positions"] * 6)
    assert w[:4] == [0.0, pacer.GAP_S, 2 * pacer.GAP_S, 3 * pacer.GAP_S]
    assert w[4:] == [pacer.QUEUE_FULL, pacer.QUEUE_FULL]   # 5s away > MAX_WAIT_S: sent at once
    assert len(r.last) == 1


def test_no_redis_means_no_pacing(monkeypatch):
    def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(pacer, "_redis", _down)
    assert pacer.wait_turn("key", "/openapi/assets/positions", sleep=lambda s: pytest.fail("slept")) == 0.0


def test_never_sleeps_on_an_event_loop_thread(r):
    async def _on_loop():
        _run(["/openapi/assets/positions"])
        return pacer.wait_turn("key", "/openapi/assets/positions", sleep=lambda s: pytest.fail("slept"),
                               clock=lambda: 1000.0)

    assert asyncio.run(_on_loop()) == 0.0


def test_the_sdk_hook_waits_its_turn_and_the_readout_says_so(r, monkeypatch):
    from webull.core.client import ApiClient

    slept = []
    monkeypatch.setattr(pacer, "wait_turn",
                        lambda h, action: (slept.append(action) or 0.4) if action.endswith("positions") else 0.0)

    def fake_single(self, endpoint, request, *a, **k):
        return (429 if request.get_action_name().endswith("balance") else 200, {}, b"", None, None)

    monkeypatch.setattr(ApiClient, "_handle_single_request", fake_single)
    monkeypatch.setattr(wu, "_installed", False)
    wu.install()

    client = ApiClient.__new__(ApiClient)
    client._app_key = "KEY"
    for action in ("/openapi/assets/positions", "/openapi/assets/balance"):
        req = type("Req", (), {"get_action_name": lambda self, a=action: a})()
        with wu.tag("P&L poller"):
            ApiClient._handle_single_request(client, None, req)

    s = wu.summary(["KEY"], minutes=5)
    assert slept == ["/openapi/assets/positions"]
    assert s["paced"] == [{"endpoint": "/openapi/assets/positions", "calls": 1, "avg_wait_ms": 400}]
    assert s["rate_limited_by"] == [{"caller": "P&L poller", "endpoint": "/openapi/assets/balance", "calls": 1}]
    assert s["total"] == 2                             # the paced counters are not counted as calls


# ── the real script, against a real Redis when one is running ─────────────────

def test_the_lua_reservation_against_redis():
    try:
        from app.services.redis_client import get_sync_redis

        real = get_sync_redis()
        real.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no Redis")
    key = f"test-{uuid.uuid4().hex}"
    try:
        waits = [pacer.wait_turn(key, "/openapi/assets/positions", sleep=lambda s: None, clock=lambda: 5000.0)
                 for _ in range(5)]
        assert waits == [0.0, pacer.GAP_S, 2 * pacer.GAP_S, 3 * pacer.GAP_S, pacer.QUEUE_FULL]
    finally:
        real.delete(f"webull:pace:{key}:positions")


def test_a_call_sent_because_the_queue_was_full_is_counted(r, monkeypatch):
    from webull.core.client import ApiClient

    monkeypatch.setattr(pacer, "wait_turn", lambda h, action: pacer.QUEUE_FULL)
    monkeypatch.setattr(ApiClient, "_handle_single_request", lambda self, e, req, *a, **k: (429, {}, b"", None, None))
    monkeypatch.setattr(wu, "_installed", False)
    wu.install()
    client = ApiClient.__new__(ApiClient)
    client._app_key = "KEY"
    req = type("Req", (), {"get_action_name": lambda self: "/openapi/assets/positions"})()
    ApiClient._handle_single_request(client, None, req)
    s = wu.summary(["KEY"], minutes=5)
    assert s["queue_full"] == [{"endpoint": "/openapi/assets/positions", "calls": 1}]
    assert s["paced"] == [] and s["total"] == 1


# ── one positions read per Discord alert ─────────────────────────────────────

def _alert(fetches, *, place_between=False):
    from app.api import discord_sources as ds
    from app.services import risk_tick

    def _fetch():
        fetches.append(1)
        return ["positions"]

    @ds._with_alert_reason
    def _execute(db, user, msg, background, request):
        for i in range(4):                             # sizing, caps, close check, …
            risk_tick.get_or_fetch("KEY:acct", _fetch)
            if place_between and i == 1:
                risk_tick.invalidate("KEY:acct")       # what a placed order does
        return "ok"

    db = type("Db", (), {"get": lambda self, *a: None})()
    msg = type("Msg", (), {"content": "SPY 700C @1.00", "source_id": None})()
    return _execute(db, None, msg, None, None)


def test_one_alert_reads_positions_once():
    """QA 2026-10-21: one of Srini's alerts made 15 Webull calls and 4 positions
    reads were refused."""
    fetches = []
    assert _alert(fetches) == "ok"
    assert len(fetches) == 1


def test_an_order_placed_in_the_alert_makes_the_next_read_live():
    fetches = []
    _alert(fetches, place_between=True)
    assert len(fetches) == 2


def test_each_alert_reads_afresh():
    fetches = []
    _alert(fetches)
    _alert(fetches)
    assert len(fetches) == 2


def test_an_outer_tick_is_kept():
    """Auto-trim submits its own alert inside the P&L poller's tick."""
    from app.services import risk_tick

    token = risk_tick.begin()
    try:
        risk_tick.get_or_fetch("KEY:acct", lambda: ["outer"])
        fetches = []
        _alert(fetches)
        assert fetches == [], "the outer tick's read is shared"
        assert risk_tick.active()
    finally:
        risk_tick.end(token)


def test_after_a_refused_close_positions_are_read_live():
    from app.services import order_retry, risk_tick

    token = risk_tick.begin()
    try:
        risk_tick.get_or_fetch("KEY:acct", lambda: ["stale"])
        assert "KEY:acct" in risk_tick._tick.get().snapshots
        req = type("Req", (), {"symbol": "SPY", "side": None, "option_right": None})()
        adapter = type("A", (), {"get_positions": lambda self: []})()
        order_retry.live_closeable_quantity(adapter, req)
        assert risk_tick._tick.get().snapshots == {}
    finally:
        risk_tick.end(token)
