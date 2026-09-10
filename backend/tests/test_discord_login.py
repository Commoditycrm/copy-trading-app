"""Tests for QR-code Discord login (production onboarding).

The property that matters most here is that a QR is a *live login credential*:
it must reach only the trader who asked for it, and it must stop existing the
moment the attempt is over. The lifecycle tests below pin both.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_login as dl


class _FakeRedis:
    """In-memory stand-in covering the string + set ops the store uses."""

    def __init__(self):
        self.kv = {}
        self.sets = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.kv[key] = value
        self.ttls[key] = ttl

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def get(self, key):
        return self.kv.get(key)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        return True

    def drop(self, key):
        """Simulate the TTL firing on one session."""
        self.kv.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(dl, "get_sync_redis", lambda: r)
    return r


def test_a_new_session_starts_pending_and_is_queued_for_the_listener(redis):
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    assert s["status"] == dl.PENDING
    assert s["qr_png"] is None
    assert [p["session_id"] for p in dl.pending()] == [s["session_id"]]


def test_storing_a_qr_marks_the_session_ready_to_scan(redis):
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    updated = dl.set_qr(s["session_id"], "BASE64PNG")
    assert updated["status"] == dl.AWAITING_SCAN
    assert updated["qr_png"] == "BASE64PNG"


def test_a_rotated_qr_replaces_the_previous_frame(redis):
    """Discord rotates the code every couple of minutes; the trader must see the
    current one, never a stale code that will fail when scanned."""
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    dl.set_qr(s["session_id"], "FRAME1")
    dl.set_qr(s["session_id"], "FRAME2")
    assert dl.get(s["session_id"])["qr_png"] == "FRAME2"


def test_finishing_a_session_destroys_the_qr(redis):
    """A completed attempt must not leave a scannable credential lying around,
    even for the minutes until its TTL would have removed it."""
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    dl.set_qr(s["session_id"], "BASE64PNG")

    done = dl.finish(s["session_id"])
    assert done["status"] == dl.COMPLETE
    assert done["qr_png"] is None


def test_a_failed_session_also_destroys_the_qr_and_keeps_the_reason(redis):
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    dl.set_qr(s["session_id"], "BASE64PNG")

    failed = dl.finish(s["session_id"], error="Discord didn't offer a QR code.")
    assert failed["status"] == dl.FAILED
    assert failed["qr_png"] is None
    assert "QR code" in failed["error"]


def test_finished_sessions_are_not_handed_to_the_listener_again(redis):
    """Otherwise a completed login would be restarted on the next poll sweep,
    opening a fresh browser for an attempt that already succeeded."""
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    dl.finish(s["session_id"])
    assert dl.pending() == []


def test_expired_sessions_are_pruned_from_the_work_queue(redis):
    """The pending set has no per-member TTL, so an id whose session has expired
    out of Redis must not accumulate as phantom work."""
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    redis.drop(f"discord:login:{s['session_id']}")

    assert dl.pending() == []
    assert redis.smembers("discord:login:pending") == set()


def test_operations_on_an_unknown_session_return_none_rather_than_raising(redis):
    ghost = uuid.uuid4()
    assert dl.get(ghost) is None
    assert dl.set_qr(ghost, "X") is None
    assert dl.finish(ghost) is None
    assert dl.set_status(ghost, dl.SCANNED) is None


def test_the_session_records_which_source_and_user_it_belongs_to(redis):
    """The poll endpoint checks both before returning a QR — handing one to the
    wrong account would let an attacker capture whoever scanned it."""
    source_id, user_id = uuid.uuid4(), uuid.uuid4()
    s = dl.create(source_id, user_id)
    assert s["source_id"] == str(source_id)
    assert s["user_id"] == str(user_id)


def test_redis_being_down_degrades_to_no_session_not_a_crash(monkeypatch):
    """A login attempt failing is recoverable (the trader retries); a 500 out of
    the settings page is not."""
    class _Down:
        def __getattr__(self, _name):
            def boom(*a, **k):
                raise ConnectionError("redis unavailable")
            return boom

    monkeypatch.setattr(dl, "get_sync_redis", lambda: _Down())
    s = dl.create(uuid.uuid4(), uuid.uuid4())
    assert s["status"] == dl.PENDING     # returned, just not persisted
    assert dl.pending() == []
    assert dl.get(s["session_id"]) is None


def test_a_second_attempt_is_refused_inside_the_cooldown(redis):
    """Back-to-back login attempts are what earn a browser an anti-bot challenge
    instead of a QR. The guard refuses the retry rather than generating traffic
    Discord is entitled to rate-limit."""
    source_id = uuid.uuid4()
    assert dl.cooldown_remaining(source_id) == 0

    dl.create(source_id, uuid.uuid4())
    assert dl.cooldown_remaining(source_id) == dl.COOLDOWN_S


def test_cooldown_is_per_source_not_global(redis):
    """One trader retrying must not lock every other source out of signing in."""
    a, b = uuid.uuid4(), uuid.uuid4()
    dl.create(a, uuid.uuid4())
    assert dl.cooldown_remaining(b) == 0
