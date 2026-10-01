"""Counting Webull requests and what made them (services/webull_usage.py)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import webull_usage as wu


class _Redis:
    def __init__(self): self.h = {}
    def hincrby(self, k, f, n): self.h.setdefault(k, {}); self.h[k][f] = self.h[k].get(f, 0) + n
    def expire(self, k, s): pass
    def hgetall(self, k): return dict(self.h.get(k, {}))


@pytest.fixture
def r(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(wu, "_redis", lambda: fake)
    return fake


def test_calls_are_counted_by_caller_endpoint_and_429(r):
    for _ in range(3):
        wu.record("KEY", "/openapi/account/positions", 200, 5, caller="Positions page")
    wu.record("KEY", "/openapi/account/positions", 429, 5, caller="Positions page")
    wu.record("KEY", "/trade/orders/list-today", 200, 5, caller="Order poll")
    wu.record("OTHER", "/trade/orders/list-today", 200, 5, caller="Order poll")   # someone else's key
    s = wu.summary(["KEY"], minutes=5)
    assert s["total"] == 5 and s["rate_limited"] == 1
    assert s["by_caller"][0] == {"caller": "Positions page", "calls": 4, "rate_limited": 1}
    assert {e["endpoint"]: e["calls"] for e in s["by_endpoint"]} == {
        "/openapi/account/positions": 4, "/trade/orders/list-today": 1}


def test_the_caller_comes_from_a_tag_or_the_thread(r):
    with wu.tag("Auto-trim"):
        assert wu.current_caller() == "Auto-trim"
    assert wu.current_caller()                     # falls back to the thread's name


def test_tagged_works_for_sync_and_async():
    @wu.tagged("P&L poller")
    def sync():
        return wu.current_caller()

    @wu.tagged("Order poll")
    async def coro():
        return await asyncio.to_thread(wu.current_caller)   # inherited by to_thread

    assert sync() == "P&L poller"
    assert asyncio.run(coro()) == "Order poll"


def test_routes_get_friendly_names():
    tok = wu.set_request_caller("GET", "/api/positions")
    assert wu.current_caller() == "Positions page"
    wu.reset_request_caller(tok)
    tok = wu.set_request_caller("POST", "/api/brokers/3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90/refresh-balance")
    assert wu.current_caller() == "Balance refresh"
    wu.reset_request_caller(tok)


def test_the_sdk_hook_counts_every_request_including_failures(r, monkeypatch):
    from webull.core.client import ApiClient

    calls = {"n": 0}

    def fake_single(self, endpoint, request, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("socket closed")
        return (429 if calls["n"] == 3 else 200, {}, b"", None, None)

    monkeypatch.setattr(ApiClient, "_handle_single_request", fake_single)
    monkeypatch.setattr(wu, "_installed", False)
    wu.install()
    client = ApiClient.__new__(ApiClient)
    client._app_key = "KEY"
    req = type("Req", (), {"get_action_name": lambda self: "/openapi/account/positions"})()
    with wu.tag("Positions page"):
        client._handle_single_request("api.sandbox.webull.com", req, 5, 5, None)
        with pytest.raises(RuntimeError):
            client._handle_single_request("api.sandbox.webull.com", req, 5, 5, None)
        client._handle_single_request("api.sandbox.webull.com", req, 5, 5, None)
    s = wu.summary(["KEY"], minutes=1)
    assert s["total"] == 3 and s["rate_limited"] == 1


def test_the_app_key_never_appears_in_the_counters(r):
    wu.record("SECRETKEY", "/x", 200, 1, caller="c")
    assert all("SECRETKEY" not in f for fields in r.h.values() for f in fields)
