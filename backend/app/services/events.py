"""Per-user event bus for SSE, backed by Redis pub/sub.

Why Redis: an SSE connection is held by exactly one FastAPI worker, but events
can be published from any worker (or from a background task running on a
different process). Redis pub/sub gives us cross-process fan-out for free.

Channel convention: `events:user:{user_id}` — one channel per recipient. We
don't multiplex; the keyspace is tiny (one channel per active SSE connection)
and per-user filtering is just a SUBSCRIBE.

Failure mode: if Redis is unreachable, publish is a no-op (event is lost) and
subscribe yields heartbeats only. The canonical state is always in Postgres,
so the SSE feed is lossy by design — a missed event just means the UI is
slightly stale until the user navigates / refetches.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)


def _channel(user_id: uuid.UUID) -> str:
    return f"events:user:{user_id}"


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """No-op now — kept for backward compatibility with main.py's startup
    hook. Redis pub/sub doesn't need a bound loop because publish is sync
    (executes synchronously against the sync redis client) and subscribe runs
    on whatever loop awaits it."""
    return None


async def subscribe(user_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
    """Subscribe to events for `user_id`. Yields decoded JSON payloads. If
    Redis is unreachable, the generator yields nothing and exits — the SSE
    endpoint's heartbeat keeps the connection alive."""
    r = get_async_redis()
    try:
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(_channel(user_id))
    except Exception:  # noqa: BLE001
        log.exception("redis pubsub subscribe failed for user=%s", user_id)
        return

    try:
        while True:
            # get_message returns None on timeout — we use that to let the
            # caller poll request.is_disconnected() between events.
            msg = await pubsub.get_message(timeout=1.0)
            if msg is None:
                continue
            data = msg.get("data")
            if data is None:
                continue
            try:
                yield json.loads(data) if isinstance(data, (str, bytes)) else data
            except json.JSONDecodeError:
                log.warning("dropping malformed event on channel %s", _channel(user_id))
    finally:
        try:
            await pubsub.unsubscribe(_channel(user_id))
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


def publish(user_id: uuid.UUID, event: dict[str, Any]) -> None:
    """Sync, fire-and-forget. Safe to call from any thread or background
    task. Drops the event silently on Redis errors."""
    try:
        get_sync_redis().publish(_channel(user_id), json.dumps(event, default=str))
    except Exception:  # noqa: BLE001
        log.warning("event publish dropped for user=%s", user_id)
