"""Channels must attach one at a time, not all at once.

Attaching renders a full Discord client and is the only genuinely CPU-heavy
thing this service does; an attached watcher is an idle DOM observer. Watchers
are all started from a single reconcile sweep, and start() only creates tasks —
so without a gate every channel attaches in the same instant.

That is not theoretical. On prod, three watchers logged "watcher started" at the
same millisecond (09:09:38,671), pinned the container's CPU limit, and each blew
the 45s channel-load timeout — which dropped them into a reconnect loop that
burned more CPU and made the next attempt likelier to fail too. Raising the CPU
limit from 1.5 to 2.5 just moved the ceiling; the burst re-saturated it.

Run: python -m pytest tests/test_connect_gate.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord_listener.config import Config
from discord_listener.watcher import ChannelWatcher


def _watcher(gate, label, events, hold=0.05):
    cfg = Config(backend_url="http://x", listener_token="t")
    w = ChannelWatcher(
        browser=object(), client=object(), config=cfg,
        assignment={
            "source_id": label, "channel_id": "123", "guild_id": "g",
            "label": label, "storage_state": {},
        },
        connect_gate=gate,
    )

    async def _attach():
        events.append(("enter", label))
        await asyncio.sleep(hold)      # stands in for the Discord page load
        events.append(("exit", label))

    w._connect_locked = _attach
    return w


def _overlapped(events):
    """True if any attach began before the previous one finished."""
    depth = 0
    for kind, _ in events:
        depth += 1 if kind == "enter" else -1
        if depth > 1:
            return True
    return False


def test_attaches_do_not_overlap():
    async def run():
        gate = asyncio.Semaphore(1)
        events = []
        ws = [_watcher(gate, f"c{i}", events) for i in range(3)]
        await asyncio.gather(*(w._connect() for w in ws))
        return events

    events = asyncio.run(run())
    assert len(events) == 6
    assert not _overlapped(events), events


def test_without_a_gate_they_all_attach_at_once():
    """Pins what the gate is actually doing — if this ever stops overlapping,
    the test above proves nothing."""
    async def run():
        events = []
        ws = [_watcher(None, f"c{i}", events) for i in range(3)]
        await asyncio.gather(*(w._connect() for w in ws))
        return events

    assert _overlapped(asyncio.run(run()))


def test_the_configured_concurrency_is_respected():
    """Two at a time is allowed, three is not."""
    async def run():
        gate = asyncio.Semaphore(2)
        events = []
        ws = [_watcher(gate, f"c{i}", events) for i in range(4)]
        await asyncio.gather(*(w._connect() for w in ws))
        return events

    events = asyncio.run(run())
    depth = peak = 0
    for kind, _ in events:
        depth += 1 if kind == "enter" else -1
        peak = max(peak, depth)
    assert peak == 2, events
