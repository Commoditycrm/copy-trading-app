"""Tests for Kopyaa Connector pairing codes.

A pairing code authorises writing a Discord session onto a source, so the
properties that matter are: single use, per-source, expiring, and useless
without the upload token handed back on claim.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_pairing as dp


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    def setex(self, key, ttl, value):
        self.kv[key] = value

    def get(self, key):
        return self.kv.get(key)

    def drop(self, key):
        self.kv.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(dp, "get_sync_redis", lambda: r)
    return r


def test_a_new_code_starts_pending_and_carries_an_upload_token(redis):
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    assert s["status"] == dp.PENDING
    assert len(s["upload_token"]) >= 16


def test_codes_avoid_visually_confusable_characters(redis):
    """The trader reads this off one screen and types it into another, so 0/O
    and 1/I/L being distinguishable is a functional requirement."""
    for _ in range(30):
        code = dp.create(uuid.uuid4(), uuid.uuid4())["code"]
        assert not (set(code) & set("01ILOU"))


@pytest.mark.parametrize(
    "typed",
    ["KPY-4F2A-9C1D", "kpy-4f2a-9c1d", "4F2A9C1D", "4f2a 9c1d", "  KPY4F2A9C1D  "],
)
def test_a_code_is_accepted_however_the_trader_types_it(redis, typed):
    assert dp.normalise(typed) == "4F2A9C1D"


def test_display_form_is_grouped_for_readability(redis):
    assert dp.format_code("4F2A9C1D") == "KPY-4F2A-9C1D"


def test_claiming_returns_the_upload_token_once(redis):
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    claimed = dp.claim(s["code"])
    assert claimed["upload_token"] == s["upload_token"]
    assert claimed["status"] == dp.CLAIMED


def test_a_code_cannot_be_claimed_twice(redis):
    """Single use is what makes a code glimpsed over a shoulder worthless once
    the real Connector has used it."""
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    assert dp.claim(s["code"]) is not None
    assert dp.claim(s["code"]) is None


def test_an_unknown_or_expired_code_cannot_be_claimed(redis):
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    redis.drop(f"discord:pair:{s['code']}")
    assert dp.claim(s["code"]) is None
    assert dp.claim("ZZZZZZZZ") is None


def test_upload_is_refused_without_the_right_token(redis):
    """The code alone must not authorise a write — otherwise anyone who saw it
    could attach a session to someone else's source."""
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    dp.claim(s["code"])
    assert dp.authorise(s["code"], "wrong-token") is None
    assert dp.authorise(s["code"], s["upload_token"]) is not None


def test_upload_is_refused_before_the_code_is_claimed(redis):
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    assert dp.authorise(s["code"], s["upload_token"]) is None


def test_finishing_destroys_the_upload_token(redis):
    """Once the session is stored there is no reason to keep a credential that
    can write to this source."""
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    dp.claim(s["code"])
    done = dp.finish(s["code"])
    assert done["status"] == dp.COMPLETE
    assert done["upload_token"] is None
    assert dp.authorise(s["code"], s["upload_token"]) is None


def test_a_failed_pairing_keeps_the_reason_and_drops_the_token(redis):
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    dp.claim(s["code"])
    failed = dp.finish(s["code"], error="The source was removed.")
    assert failed["status"] == dp.FAILED
    assert failed["error"] == "The source was removed."
    assert failed["upload_token"] is None


def test_the_code_records_which_source_and_user_it_belongs_to(redis):
    """Both poll and complete check the source matches before acting."""
    source_id, user_id = uuid.uuid4(), uuid.uuid4()
    s = dp.create(source_id, user_id)
    assert s["source_id"] == str(source_id)
    assert s["user_id"] == str(user_id)


def test_codes_are_unpredictable(redis):
    codes = {dp.create(uuid.uuid4(), uuid.uuid4())["code"] for _ in range(200)}
    assert len(codes) == 200


def test_redis_being_down_degrades_to_no_pairing_not_a_crash(monkeypatch):
    class _Down:
        def __getattr__(self, _n):
            def boom(*a, **k):
                raise ConnectionError("redis unavailable")
            return boom

    monkeypatch.setattr(dp, "get_sync_redis", lambda: _Down())
    s = dp.create(uuid.uuid4(), uuid.uuid4())
    assert s["status"] == dp.PENDING     # returned, just not persisted
    assert dp.get(s["code"]) is None
    assert dp.claim(s["code"]) is None
