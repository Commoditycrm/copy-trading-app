"""Cross-process (worker vs backend) Webull positions coordination.

The display path (Positions page, cached_ok=True) must not fire a second LIVE
Webull /assets/positions read into the risk worker's — or another tab's — burst
window. It reuses a very recent shared snapshot even for a "fresh" read, and a
Redis per-account single-flight lets only one process refresh at a time. The
risk path (cached_ok=False) is untouched. Webull-only. No real Redis/broker —
the snapshot store and the Redis lock are faked, so these are deterministic.
"""
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.brokers import webull as wb


class FakeRedis:
    """Minimal Redis for the single-flight lock (SET NX EX / GET / DELETE)."""

    def __init__(self):
        self.d = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    def get(self, k):
        return self.d.get(k)

    def delete(self, k):
        self.d.pop(k, None)
        return 1


@pytest.fixture
def env(monkeypatch):
    """Fake Redis for the lock + an in-memory snapshot store (bypasses JSON)."""
    import app.services.redis_client as rc
    fake = FakeRedis()
    monkeypatch.setattr(rc, "get_sync_redis", lambda: fake)
    wb._positions_cache.clear()            # module-global in-proc cache: isolate tests

    store = {}  # (app_key, account_id) -> (stored_at, fresh, positions)

    def _write(ak, ac, pos):
        store[(ak, ac)] = (time.monotonic(), True, list(pos))

    def _read(ak, ac):
        s = store.get((ak, ac))
        if s is None:
            return None
        return (time.monotonic() - s[0], s[1], s[2])

    monkeypatch.setattr(wb, "_snapshot_write", _write)
    monkeypatch.setattr(wb, "_snapshot_read", _read)

    def seed(ak, ac, age, fresh, positions):
        store[(ak, ac)] = (time.monotonic() - age, fresh, list(positions))

    def lock_key(ak, ac):
        return wb._REFRESH_LOCK_KEY.format(wb._snapshot_id(ak, ac))

    return types.SimpleNamespace(redis=fake, store=store, seed=seed, lock_key=lock_key)


def _adapter(monkeypatch, counter, app_key="k1", account_id="a1"):
    a = wb.WebullAdapter.__new__(wb.WebullAdapter)
    a.app_key = app_key
    a.account_id = account_id

    def _fetch():
        counter["n"] += 1
        return [("pos", counter["n"])]

    monkeypatch.setattr(a, "_fetch_positions", _fetch)
    return a


# ─────────────── fresh read reuses a very-recent snapshot ───────────────
def test_fresh_reuses_recent_snapshot(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    env.seed("k1", "a1", age=1.0, fresh=True, positions=["worker-read"])
    out = b.get_positions(cached_ok=True, fresh=True)
    assert out == ["worker-read"]
    assert c["n"] == 0                     # no Webull call — reused worker's snapshot


def test_fresh_fetches_when_snapshot_too_old(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    env.seed("k1", "a1", age=5.0, fresh=True, positions=["old"])   # > _DISPLAY_FRESH_REUSE_S
    b.get_positions(cached_ok=True, fresh=True)
    assert c["n"] == 1                     # stale window → live read


def test_fresh_fetches_when_snapshot_marked_stale_by_fill(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    env.seed("k1", "a1", age=1.0, fresh=False, positions=["pre-fill"])  # a fill marked it stale
    b.get_positions(cached_ok=True, fresh=True)
    assert c["n"] == 1                     # never reuse a post-fill stale snapshot as fresh


def test_nonfresh_reuses_up_to_10s(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    env.seed("k1", "a1", age=8.0, fresh=True, positions=["recent"])
    out = b.get_positions(cached_ok=True)   # not fresh → _SNAPSHOT_FRESH_S window
    assert out == ["recent"] and c["n"] == 0


# ─────────────── cross-process single-flight ───────────────
def test_waiter_reuses_when_another_process_holds_the_lock(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    # Another process is mid-refresh: its lock is held and it has just written a
    # snapshot too old to reuse directly (2.8s) but within the lock TTL (3.0s).
    env.redis.set(env.lock_key("k1", "a1"), "1")
    env.seed("k1", "a1", age=2.8, fresh=True, positions=["refresher-result"])
    out = b.get_positions(cached_ok=True, fresh=True)
    assert out == ["refresher-result"]
    assert c["n"] == 0                     # waited for the in-flight refresh, no Webull call


def test_lock_free_fetches_writes_and_releases(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    out = b.get_positions(cached_ok=True, fresh=True)   # no snapshot, lock free
    assert c["n"] == 1                                  # one live read
    assert env.store.get(("k1", "a1")) is not None      # snapshot written
    assert env.lock_key("k1", "a1") not in env.redis.d  # lock released


def test_concurrent_display_reads_one_webull_call(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)

    def _slow_fetch():
        c["n"] += 1
        time.sleep(0.15)                    # hold the refresh so others overlap
        return ["shared"]

    monkeypatch.setattr(b, "_fetch_positions", _slow_fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(b.get_positions(cached_ok=True, fresh=True)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c["n"] == 1                      # 5 tabs → ONE Webull call
    assert all(r == ["shared"] for r in results)


def test_different_accounts_do_not_block(env, monkeypatch):
    c = {"n": 0}
    b1 = _adapter(monkeypatch, c, account_id="a1")
    b2 = _adapter(monkeypatch, c, account_id="a2")
    b1.get_positions(cached_ok=True, fresh=True)
    b2.get_positions(cached_ok=True, fresh=True)
    assert c["n"] == 2                      # independent per-account refresh


# ─────────────── failure modes ───────────────
def test_redis_down_falls_back_to_direct_fetch(env, monkeypatch):
    import app.services.redis_client as rc

    def _boom():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(rc, "get_sync_redis", _boom)
    monkeypatch.setattr(wb, "_snapshot_read", lambda *a: None)   # snapshot layer also down
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    out = b.get_positions(cached_ok=True, fresh=True)
    assert out == [("pos", 1)] and c["n"] == 1   # display still works via a direct read


def test_rate_limit_returns_stale_snapshot(env, monkeypatch):
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    env.seed("k1", "a1", age=50.0, fresh=True, positions=["last-known"])  # too old to reuse

    def _fetch_429():
        c["n"] += 1
        raise RuntimeError("HTTP Status: 429, TOO_MANY_REQUESTS")

    monkeypatch.setattr(b, "_fetch_positions", _fetch_429)
    out = b.get_positions(cached_ok=True, fresh=True)
    assert list(out) == ["last-known"]
    assert isinstance(out, wb.StalePositions) and out.stale_age_s >= 50
    assert c["n"] == 1                      # one attempt, no retry storm


# ─────────────── scope guards ───────────────
def test_risk_path_does_not_use_the_display_coordinator(env, monkeypatch):
    # cached_ok=False must NOT go through the cross-container coordinator.
    monkeypatch.setattr(wb, "_coordinated_display_fetch",
                        lambda *a, **k: pytest.fail("risk path used the display coordinator"))
    monkeypatch.setattr(wb, "_snapshot_write", lambda *a, **k: None)
    c = {"n": 0}
    b = _adapter(monkeypatch, c)
    b.get_positions()                       # cached_ok=False
    assert c["n"] == 1                      # one live read via the risk path


def test_coordination_is_webull_only():
    import inspect
    import app.brokers.alpaca as alpaca
    src = inspect.getsource(alpaca)
    assert "_coordinated_display_fetch" not in src and "_REFRESH_LOCK_KEY" not in src
