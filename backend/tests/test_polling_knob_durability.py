"""An admin's polling override must outlive the cache it is read through.

Redis holds these overrides, but QA runs it as a pure cache — `--save ""`,
`--appendonly no`, `allkeys-lru`. A restart or memory pressure dropped every
key, and because a cache MISS was indistinguishable from "no override set",
the pollers silently reverted to their env defaults and the admin page showed
"-" as though nobody had ever configured anything.

app_settings is now the source of truth and Redis the read cache. These tests
are about that distinction: what survives, what the hot path is still allowed
to skip, and that a miss is no longer mistaken for an answer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.platform_config as pc


class _FakeRedis:
    def __init__(self, data=None, boom=False):
        self.data = dict(data or {})
        self.boom = boom

    def get(self, key):
        if self.boom:
            raise RuntimeError("redis down")
        return self.data.get(key)

    def set(self, key, value):
        if self.boom:
            raise RuntimeError("redis down")
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def store(monkeypatch):
    """Redis and the database, both faked, with the in-process cache cleared so
    one test's read cannot answer another's."""
    redis = _FakeRedis()
    rows: dict[str, str] = {}
    monkeypatch.setattr(pc, "get_sync_redis", lambda: redis)
    monkeypatch.setattr(pc, "_db_get", lambda key: rows.get(key))

    def _set(key, value):
        if value is None:
            rows.pop(key, None)
        else:
            rows[key] = value
        pc._db_cache.pop(key, None)

    monkeypatch.setattr(pc, "_db_set", _set)
    pc._db_cache.clear()
    return redis, rows


KNOB = "alpaca_pnl_poll_interval_s"


def _default():
    from app.config import get_settings
    return get_settings().alpaca_pnl_poll_interval_s


# ── the bug this fixes ───────────────────────────────────────────────────────

def test_an_override_survives_the_cache_being_wiped(store):
    redis, rows = store
    pc.set_int_knob(KNOB, 42)
    redis.data.clear()                      # Redis restarted / evicted
    assert pc.get_int_knob(KNOB) == 42, "the override must come back from the database"


def test_a_wiped_cache_is_rewarmed_by_the_read(store):
    redis, rows = store
    pc.set_int_knob(KNOB, 42)
    key = pc._INT_BY_NAME[KNOB].key
    redis.data.clear()
    pc._db_cache.clear()
    pc.get_int_knob(KNOB)
    assert redis.data.get(key) == "42", "a database hit should warm the cache"


def test_a_dead_cache_still_serves_the_configured_value(store):
    redis, rows = store
    pc.set_int_knob(KNOB, 42)
    redis.boom = True                        # every Redis call raises
    pc._db_cache.clear()
    assert pc.get_int_knob(KNOB) == 42


def test_no_override_anywhere_is_still_the_env_default(store):
    assert pc.get_int_knob(KNOB) == _default()


# ── writes land durably ──────────────────────────────────────────────────────

def test_a_write_reaches_the_database_not_just_the_cache(store):
    redis, rows = store
    pc.set_int_knob(KNOB, 7)
    assert rows[pc._INT_BY_NAME[KNOB].key] == "7"


def test_clearing_removes_it_from_both(store):
    redis, rows = store
    key = pc._INT_BY_NAME[KNOB].key
    pc.set_int_knob(KNOB, 7)
    pc.set_int_knob(KNOB, None)
    assert key not in rows and key not in redis.data
    assert pc.get_int_knob(KNOB) == _default()


def test_bounds_are_still_enforced_before_anything_is_stored(store):
    redis, rows = store
    with pytest.raises(ValueError):
        pc.set_int_knob(KNOB, 99999)
    assert rows == {} and redis.data == {}


def test_a_switch_survives_the_cache_too(store):
    redis, rows = store
    name = pc.BOOL_KNOBS[0].name
    pc.set_bool_knob(name, True)
    redis.data.clear()
    pc._db_cache.clear()
    assert pc.get_bool_knob(name) is True


# ── the admin page tells the truth after a restart ───────────────────────────

def test_the_admin_page_still_shows_the_override_after_a_wipe(store):
    redis, rows = store
    pc.set_int_knob(KNOB, 42)
    redis.data.clear()
    pc._db_cache.clear()
    state = pc.get_polling_knobs_state()
    row = next(r for g in state.values() if isinstance(g, list)
               for r in g if r.get("name") == KNOB)
    assert row["override"] == 42, "a wiped cache must not read as 'never configured'"


# ── the legacy door agrees ───────────────────────────────────────────────────

def test_the_legacy_alpaca_door_is_durable_too(store):
    redis, rows = store
    pc.set_alpaca_pnl_poll_interval(23)
    redis.data.clear()
    pc._db_cache.clear()
    assert pc.get_alpaca_pnl_poll_interval_sync() == 23
    assert pc.get_int_knob(KNOB) == 23, "both doors read one value"


# ── carrying existing overrides over on first deploy ─────────────────────────

def test_backfill_carries_a_cache_only_override_into_the_database(store):
    redis, rows = store
    key = pc._INT_BY_NAME[KNOB].key
    redis.data[key] = "15"                   # set before this change shipped
    assert pc.backfill_overrides_from_cache() >= 1
    assert rows[key] == "15"


def test_backfill_never_overwrites_what_is_already_stored(store):
    redis, rows = store
    key = pc._INT_BY_NAME[KNOB].key
    rows[key] = "30"
    redis.data[key] = "15"
    pc.backfill_overrides_from_cache()
    assert rows[key] == "30", "the durable copy wins"


def test_backfill_is_idempotent(store):
    redis, rows = store
    redis.data[pc._INT_BY_NAME[KNOB].key] = "15"
    first = pc.backfill_overrides_from_cache()
    second = pc.backfill_overrides_from_cache()
    assert first >= 1 and second == 0


def test_backfill_survives_a_dead_cache(store):
    redis, rows = store
    redis.boom = True
    assert pc.backfill_overrides_from_cache() == 0


# ── the hot path stays off the database, deliberately ────────────────────────

def test_the_async_fanout_getter_never_blocks_on_the_database(store, monkeypatch):
    """It reads the cache only — a blocking query on the event loop during a
    fanout would stall every subscriber's order. Correctness comes from the
    cache being warmed by the sync path and the startup backfill instead."""
    import asyncio

    redis, rows = store
    monkeypatch.setattr(pc, "_db_get", lambda key: pytest.fail("blocked on the database"))

    class _AsyncRedis:
        async def get(self, key):
            return None

    monkeypatch.setattr(pc, "get_async_redis", lambda: _AsyncRedis())
    from app.config import get_settings
    got = asyncio.run(pc.get_fanout_batch_threshold_async())
    assert got == get_settings().fanout_batch_threshold


# ── end to end: a stored override reaches the loop that polls ────────────────

def test_the_poller_uses_a_value_that_exists_only_in_the_database(store, monkeypatch):
    """The whole point, in one test: an admin sets an interval, the cache is
    then lost, and the P&L loop still polls at the configured cadence rather
    than silently reverting to the env default.
    """
    from app.models.broker_account import BrokerName
    from app.services import market_hours as mh, pnl_poller

    redis, rows = store
    monkeypatch.setattr(mh, "is_tradable_now", lambda *a, **k: True)

    pc.set_int_knob(KNOB, 23)
    redis.data.clear()                       # Redis restarted
    pc._db_cache.clear()

    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == 23.0


def test_changing_the_knob_changes_the_next_poll_without_a_restart(store, monkeypatch):
    """A change lands on the next tick: the loop re-reads per tick rather than
    capturing the interval at startup."""
    from app.models.broker_account import BrokerName
    from app.services import market_hours as mh, pnl_poller

    redis, rows = store
    monkeypatch.setattr(mh, "is_tradable_now", lambda *a, **k: True)

    pc.set_int_knob(KNOB, 30)
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == 30.0
    pc.set_int_knob(KNOB, 7)                 # admin edits the page
    pc._db_cache.clear()                     # (the TTL would expire on its own)
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == 7.0


def test_clearing_the_override_returns_the_loop_to_the_env_default(store, monkeypatch):
    from app.models.broker_account import BrokerName
    from app.services import market_hours as mh, pnl_poller

    redis, rows = store
    monkeypatch.setattr(mh, "is_tradable_now", lambda *a, **k: True)

    pc.set_int_knob(KNOB, 7)
    pc.set_int_knob(KNOB, None)
    pc._db_cache.clear()
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == float(_default())
