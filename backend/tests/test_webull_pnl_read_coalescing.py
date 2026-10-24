"""Risk-tick coalescing of Webull LIVE positions reads.

Inside one pnl_poller enforcement tick the sub-enforcers (option-SL monitor,
Discord stop reconcile, emulated trailing stops) each read the SAME account's
positions LIVE within ~0.2s, and Webull 429s the 3rd. These tests prove the
reads are shared within a tick, stay fresh on each NEW tick, re-read LIVE after
a mutating place_order, are unchanged outside a tick, that the DISPLAY path is
untouched, and that Alpaca is not involved.

No network / broker SDK: ``_fetch_positions`` and the snapshot writers are
stubbed, so these are deterministic.
"""
import inspect
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.brokers import webull as wb
from app.models.order import InstrumentType
from app.services import risk_tick


@pytest.fixture(autouse=True)
def _reset_tick():
    # Guarantee each test starts and ends outside a tick, even if a prior test
    # failed mid-tick (the ContextVar lives for the whole process).
    risk_tick._tick.set(None)
    yield
    risk_tick._tick.set(None)


def _make(app_key="k1", account_id="a1"):
    a = wb.WebullAdapter.__new__(wb.WebullAdapter)
    a.app_key = app_key
    a.account_id = account_id
    return a


def _broker(monkeypatch, counter, app_key="k1", account_id="a1"):
    monkeypatch.setattr(wb, "_snapshot_write", lambda *a, **k: None)
    b = _make(app_key, account_id)

    def _fetch():
        counter["n"] += 1
        return [("snapshot", counter["n"])]

    monkeypatch.setattr(b, "_fetch_positions", _fetch)
    return b


# ─────────────────────────── risk_tick unit ───────────────────────────
def test_get_or_fetch_outside_tick_always_fetches():
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        return calls["n"]

    assert risk_tick.active() is False
    assert risk_tick.get_or_fetch("k", f) == 1
    assert risk_tick.get_or_fetch("k", f) == 2   # no tick → no sharing
    assert calls["n"] == 2


def test_tick_shares_one_fetch_and_counts():
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        return object()

    tok = risk_tick.begin()
    a = risk_tick.get_or_fetch("acct", f)
    b = risk_tick.get_or_fetch("acct", f)
    c = risk_tick.get_or_fetch("acct", f)
    t = risk_tick.end(tok)

    assert a is b is c
    assert calls["n"] == 1
    assert (t.fresh_fetches, t.reuses, t.refreshes) == (1, 2, 0)


def test_invalidate_forces_exactly_one_refresh():
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        return calls["n"]

    tok = risk_tick.begin()
    assert risk_tick.get_or_fetch("acct", f) == 1
    risk_tick.invalidate("acct")                      # a mutation happened
    assert risk_tick.get_or_fetch("acct", f) == 2     # fresh again
    assert risk_tick.get_or_fetch("acct", f) == 2     # reused after the refresh
    t = risk_tick.end(tok)

    assert calls["n"] == 2
    assert (t.fresh_fetches, t.reuses, t.refreshes) == (2, 1, 1)


def test_each_new_tick_refetches():
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        return calls["n"]

    tok = risk_tick.begin(); risk_tick.get_or_fetch("acct", f); risk_tick.end(tok)
    tok = risk_tick.begin(); risk_tick.get_or_fetch("acct", f); risk_tick.end(tok)

    assert calls["n"] == 2            # a NEW tick re-fetches; never reuse tick 1 forever
    assert risk_tick.active() is False


def test_invalidate_and_get_or_fetch_are_noops_outside_tick():
    risk_tick.invalidate("acct")      # must not raise
    calls = {"n": 0}
    risk_tick.get_or_fetch("acct", lambda: calls.__setitem__("n", 1))
    assert calls["n"] == 1


# ─────────────────────── Webull adapter integration ───────────────────────
def test_webull_live_reads_coalesce_within_a_tick(monkeypatch):
    c = {"n": 0}
    b = _broker(monkeypatch, c)
    tok = risk_tick.begin()
    r1 = b.get_positions()           # cached_ok=False (default) = the risk path
    r2 = b.get_positions()
    r3 = b.get_positions()
    risk_tick.end(tok)

    assert r1 is r2 is r3
    assert c["n"] == 1               # 3 sub-enforcer reads → ONE Webull call


def test_webull_live_reads_fresh_each_new_tick(monkeypatch):
    c = {"n": 0}
    b = _broker(monkeypatch, c)
    tok = risk_tick.begin(); b.get_positions(); risk_tick.end(tok)
    tok = risk_tick.begin(); b.get_positions(); risk_tick.end(tok)
    assert c["n"] == 2               # fresh per tick


def test_webull_live_reads_unchanged_outside_a_tick(monkeypatch):
    c = {"n": 0}
    b = _broker(monkeypatch, c)
    b.get_positions()
    b.get_positions()
    assert c["n"] == 2               # no tick → each read hits Webull, as before


def test_distinct_accounts_do_not_share_within_a_tick(monkeypatch):
    c = {"n": 0}
    monkeypatch.setattr(wb, "_snapshot_write", lambda *a, **k: None)
    b1 = _make("k", "a1")
    b2 = _make("k", "a2")
    monkeypatch.setattr(b1, "_fetch_positions", lambda: c.__setitem__("n", c["n"] + 1) or ["a1"])
    monkeypatch.setattr(b2, "_fetch_positions", lambda: c.__setitem__("n", c["n"] + 1) or ["a2"])
    tok = risk_tick.begin()
    b1.get_positions(); b1.get_positions()
    b2.get_positions(); b2.get_positions()
    risk_tick.end(tok)
    assert c["n"] == 2               # one fresh read per distinct account, reused after


def test_place_order_invalidates_tick_snapshot(monkeypatch):
    c = {"n": 0}
    b = _broker(monkeypatch, c)
    # Stub the SDK-touching parts of a stock place_order.
    monkeypatch.setattr(b, "_client_order_id", lambda req: "coid")
    monkeypatch.setattr(b, "_build_stock_order", lambda req, coid: {})
    monkeypatch.setattr(b, "_assert_place_accepted", lambda resp, coid: None)
    monkeypatch.setattr(
        b, "_trade_client",
        lambda: SimpleNamespace(order_v3=SimpleNamespace(place_order=lambda a, o: {"ok": 1})),
    )
    req = SimpleNamespace(instrument_type=InstrumentType.STOCK)

    tok = risk_tick.begin()
    b.get_positions()                # fresh read #1
    b.get_positions()                # reuse
    b.place_order(req)               # mutation → invalidate
    b.get_positions()                # must re-read LIVE
    t = risk_tick.end(tok)

    assert c["n"] == 2               # one read before, one after the mutation
    assert t.refreshes == 1


def test_display_reads_do_not_use_the_risk_tick(monkeypatch):
    # cached_ok=True is the DISPLAY path (its own 2s/10s cache). It must NOT be
    # coalesced through the risk tick — the tick only touches cached_ok=False.
    c = {"n": 0}
    monkeypatch.setattr(wb, "_positions_cache", {})
    monkeypatch.setattr(wb, "_snapshot_read", lambda *a, **k: None)
    monkeypatch.setattr(wb, "_snapshot_write", lambda *a, **k: None)
    b = _make("kD", "aD")
    monkeypatch.setattr(b, "_fetch_positions", lambda: c.__setitem__("n", c["n"] + 1) or ["p"])

    tok = risk_tick.begin()
    b.get_positions(cached_ok=True)  # display path
    t = risk_tick.end(tok)

    assert c["n"] == 1
    assert (t.fresh_fetches, t.reuses) == (0, 0)   # display never touches the tick


# ─────────────────────── Alpaca regression ───────────────────────
def test_alpaca_adapter_not_involved_in_coalescing():
    # Coalescing is Webull-only; Alpaca must be untouched by this PR.
    import app.brokers.alpaca as alpaca
    assert "risk_tick" not in inspect.getsource(alpaca)
