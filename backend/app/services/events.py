"""In-process per-user event bus for SSE.

Sync `publish` (called from background threads / sync code) hands events to
async consumers via per-subscriber asyncio.Queue. Lossy: if a queue is full or
no consumer is connected, events are dropped silently — acceptable for "live
order feed" UX since the canonical state is always in Postgres.

Single-process only. For multi-process / multi-host deployment, swap the
in-memory dict for Redis pub/sub (interface stays identical).
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

_subscribers: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)
_loop: asyncio.AbstractEventLoop | None = None

QUEUE_MAX = 100


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at app startup so background threads can hand events back
    to the right asyncio loop via call_soon_threadsafe."""
    global _loop
    _loop = loop


async def subscribe(user_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _subscribers[user_id].add(q)
    try:
        while True:
            event = await q.get()
            yield event
    finally:
        _subscribers[user_id].discard(q)
        if not _subscribers[user_id]:
            _subscribers.pop(user_id, None)


def publish(user_id: uuid.UUID, event: dict[str, Any]) -> None:
    """Sync, thread-safe. Drops the event if the queue is full or there's no
    bound loop yet (e.g. during tests)."""
    queues = _subscribers.get(user_id)
    if not queues:
        return
    if _loop is None:
        for q in list(queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return
    for q in list(queues):
        _loop.call_soon_threadsafe(_safe_put, q, event)


def _safe_put(q: asyncio.Queue, event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass
