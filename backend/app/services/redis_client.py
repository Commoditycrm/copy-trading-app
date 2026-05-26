"""Shared Redis clients (async + sync).

We need both shapes:
  - async: pub/sub for SSE, async cache reads from FastAPI handlers and the
    async fanout loop.
  - sync: cache invalidations from sync code paths (SQLAlchemy hooks, the
    existing copy_engine helpers that haven't been awaited yet).

Both share the same connection pool URL so they hit the same Redis instance.
If Redis is unreachable, callers should degrade gracefully — caches fall back
to the DB, pub/sub publish is a no-op. Never let Redis being down take the
whole app down.

Loop affinity (async client)
----------------------------
``aioredis.from_url`` builds connections that are tied to the event loop
they're first used on. When uvicorn ``--reload`` swaps in a new loop on
code change, our module-level cache still points at the old client →
every subsequent ``await pubsub.get_message()`` raises ``RuntimeError:
Future attached to a different loop``. We cache the client *per loop* so
the first request after a reload silently rebuilds it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from weakref import WeakValueDictionary

import redis
import redis.asyncio as aioredis

from app.config import get_settings

log = logging.getLogger(__name__)

# Map id(loop) → async client. WeakValueDictionary would be nicer but
# aioredis.Redis isn't weakref-able; we GC old entries on cache miss
# instead (cheap — there's never more than ~2 loops alive at once during
# a reload window).
_async_clients: dict[int, aioredis.Redis] = {}
_sync_client: Optional[redis.Redis] = None


def get_async_redis() -> aioredis.Redis:
    """Return a per-loop async Redis client. Reuses the cached client on
    the current loop; otherwise builds a fresh one and prunes stale
    entries from dead loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. called from sync test setup). Fall back
        # to the legacy single-instance behaviour — caller bears the risk.
        if not _async_clients:
            s = get_settings()
            _async_clients[0] = aioredis.from_url(
                s.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=30,
            )
        return next(iter(_async_clients.values()))

    key = id(loop)
    client = _async_clients.get(key)
    if client is not None:
        return client

    # Cache miss — build a new client and prune any stale entries from
    # loops that have been closed (which is what happens on uvicorn
    # --reload). Skipping the prune isn't catastrophic; it just leaks
    # one Redis client per reload until process exit.
    for stale_key in list(_async_clients):
        # asyncio doesn't expose a clean is-closed check across versions,
        # so we infer staleness from id-not-matching-any-current-loop.
        # Since we only ever have the current loop here, dropping all
        # non-matching entries is safe.
        if stale_key != key:
            _async_clients.pop(stale_key, None)

    s = get_settings()
    client = aioredis.from_url(
        s.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    _async_clients[key] = client
    return client


def get_sync_redis() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        s = get_settings()
        _sync_client = redis.from_url(
            s.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _sync_client


async def close_async_redis() -> None:
    """Close every cached async client. Called from FastAPI shutdown."""
    for key, client in list(_async_clients.items()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            log.exception("error closing async redis (loop=%s)", key)
        _async_clients.pop(key, None)
