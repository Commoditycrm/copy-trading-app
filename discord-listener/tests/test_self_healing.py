"""Channels recover without being toggled off and on.

Symptom (QA, 2026-10): a channel would stop reading at random and stay down
until the trader switched it off and on, after which the same session connected
fine. Cause: seeing Discord's /login route once — which the client passes
through while it reloads, e.g. after a renderer crash under memory pressure —
was taken as a sign-out; the watcher stopped for good, and the runner never
restarted a watcher that was still in its map.

Run: python -m pytest tests/test_self_healing.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from discord_listener import watcher as wmod
from discord_listener.config import Config
from discord_listener.runner import ListenerRunner
from discord_listener.watcher import ChannelWatcher

CHANNEL = "https://discord.com/channels/g/123"


def _cfg(**kw):
    return Config(backend_url="http://x", listener_token="t", session_recheck_s=0, **kw)


def _assignment(sid="s1"):
    return {"source_id": sid, "channel_id": "123", "guild_id": "g", "label": sid, "storage_state": {}}


class _Page:
    def __init__(self, urls):
        self._urls = list(urls)
        self.url = self._urls.pop(0)
        self.gotos = 0

    async def goto(self, url, **kw):
        self.gotos += 1
        if self._urls:
            self.url = self._urls.pop(0)

    def is_closed(self):
        return False


def _watcher(page, **cfg):
    w = ChannelWatcher(browser=object(), client=object(), config=_cfg(**cfg), assignment=_assignment())
    w._page = page
    return w


# ── a login-page bounce is re-checked before it counts ─────────────────────

def test_a_momentary_login_bounce_is_not_a_sign_out():
    page = _Page(["https://discord.com/login", CHANNEL])        # reload passes through /login
    asyncio.run(_watcher(page)._verify_session())                # no raise
    assert page.gotos == 1


def test_a_login_page_that_stays_is_a_sign_out():
    page = _Page(["https://discord.com/login", "https://discord.com/login"])
    with pytest.raises(wmod._SessionExpired):
        asyncio.run(_watcher(page)._verify_session())


def test_the_channel_page_needs_no_recheck():
    page = _Page([CHANNEL])
    asyncio.run(_watcher(page)._verify_session())
    assert page.gotos == 0


# ── the page is recycled after its max age ─────────────────────────────────

def test_an_old_page_is_recycled():
    page = _Page([CHANNEL])
    w = _watcher(page, heartbeat_interval_s=0)
    w._max_age_s = 10
    w._connected_at = time.monotonic() - 11
    with pytest.raises(wmod._Recycle):
        asyncio.run(w._supervise())


def test_a_recycle_reconnects_without_reporting_an_error():
    statuses, connects = [], []

    class _Client:
        async def post_status(self, sid, status, **kw):
            statuses.append(status)

    w = ChannelWatcher(browser=object(), client=_Client(), config=_cfg(), assignment=_assignment())

    async def _connect():
        connects.append(1)

    async def _supervise():
        if len(connects) == 1:
            raise wmod._Recycle()
        w._stopping.set()

    async def _close():
        pass

    w._connect, w._supervise, w._close_context = _connect, _supervise, _close
    asyncio.run(w._run())
    assert len(connects) == 2 and "error" not in statuses
    assert not w.is_finished()                      # stopped by request, not on its own


# ── a stopped watcher is restarted by the runner ────────────────────────────

class _FakeWatcher:
    started = []

    def __init__(self, browser, client, config, assignment, connect_gate=None):
        self.source_id = assignment["source_id"]
        self.label = assignment["label"]
        self.finished_at = None
        self.stopped = False

    def matches(self, a):
        return True

    def is_finished(self):
        return self.finished_at is not None

    async def start(self):
        _FakeWatcher.started.append(self)

    async def stop(self, report=True):
        self.stopped = True


def _runner(monkeypatch, restart_after=300):
    import discord_listener.runner as rmod

    monkeypatch.setattr(rmod, "ChannelWatcher", _FakeWatcher)
    _FakeWatcher.started = []
    r = ListenerRunner(_cfg(restart_stopped_after_s=restart_after))

    class _Client:
        async def fetch_assignments(self):
            return [_assignment()]

        async def post_status(self, *a, **k):
            pass

    r._client = _Client()
    return r


def test_a_stopped_watcher_is_restarted_after_the_wait(monkeypatch):
    r = _runner(monkeypatch, restart_after=300)

    async def run():
        await r._reconcile(object())
        first = r._watchers["s1"]
        first.finished_at = time.monotonic() - 301      # it gave up five minutes ago
        await r._reconcile(object())
        return first, r._watchers["s1"]

    first, now = asyncio.run(run())
    assert first.stopped and now is not first and len(_FakeWatcher.started) == 2


def test_it_waits_before_restarting(monkeypatch):
    r = _runner(monkeypatch, restart_after=300)

    async def run():
        await r._reconcile(object())
        r._watchers["s1"].finished_at = time.monotonic() - 10
        await r._reconcile(object())

    asyncio.run(run())
    assert len(_FakeWatcher.started) == 1


def test_a_running_watcher_is_left_alone(monkeypatch):
    r = _runner(monkeypatch)

    async def run():
        await r._reconcile(object())
        await r._reconcile(object())

    asyncio.run(run())
    assert len(_FakeWatcher.started) == 1
