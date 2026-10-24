"""GET /api/positions/day-pnl is shared for a few seconds across tabs/processes.

Each call is a broker read (Webull: 2 per 2s per key). Uncached, every open
Positions tab and every order-event refresh asked again — ~500 Webull calls an
hour on QA, a sixth refused (2026-10-06).
"""
import uuid

import pytest

import app.api.positions as positions
from app.services import redis_client


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return False
        self.kv[k] = v if isinstance(v, (bytes, str)) else str(v)
        return True

    def delete(self, k):
        self.kv.pop(k, None)


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_sync_redis", lambda: r)
    return r


def test_the_broker_is_asked_once_for_a_burst(redis):
    calls = []

    def compute():
        calls.append(1)
        return {"day_pnl": 12.5, "day_pnl_pct": 1.2, "source": "webull", "quality": "authoritative"}

    user = uuid.uuid4()
    outs = [positions._shared_day_pnl(user, compute) for _ in range(4)]
    assert len(calls) == 1 and all(o == outs[0] for o in outs)
    assert outs[0]["day_pnl"] == 12.5


def test_each_user_has_their_own(redis):
    calls = []
    for _ in range(2):
        positions._shared_day_pnl(uuid.uuid4(), lambda: calls.append(1) or {"day_pnl": 1.0})
    assert len(calls) == 2


def test_a_request_that_finds_another_computing_waits_for_its_answer(redis, monkeypatch):
    user = uuid.uuid4()
    key = positions._day_pnl_key(user)
    redis.kv[key + ":lock"] = "1"                         # someone else is asking the broker
    sleeps = []

    def _sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:                             # …and their answer lands
            redis.kv[key] = '{"day_pnl": 7.0}'

    monkeypatch.setattr(positions.time, "sleep", _sleep)
    out = positions._shared_day_pnl(user, lambda: pytest.fail("must not ask the broker too"))
    assert out == {"day_pnl": 7.0}


def test_without_redis_it_still_answers(monkeypatch):
    def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_sync_redis", _down)
    assert positions._shared_day_pnl(uuid.uuid4(), lambda: {"day_pnl": 3.0}) == {"day_pnl": 3.0}


def test_a_failed_read_frees_the_lock(redis):
    user = uuid.uuid4()

    def boom():
        raise RuntimeError("TOO_MANY_REQUESTS")

    with pytest.raises(RuntimeError):
        positions._shared_day_pnl(user, boom)
    assert positions._day_pnl_key(user) + ":lock" not in redis.kv
