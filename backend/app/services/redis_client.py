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
"""
from __future__ import annotations

import logging
from typing import Optional

import redis
import redis.asyncio as aioredis

from app.config import get_settings

log = logging.getLogger(__name__)

_async_client: Optional[aioredis.Redis] = None
_sync_client: Optional[redis.Redis] = None


def get_async_redis() -> aioredis.Redis:
    global _async_client
    if _async_client is None:
        s = get_settings()
        _async_client = aioredis.from_url(
            s.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _async_client


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
    global _async_client
    if _async_client is not None:
        try:
            await _async_client.aclose()
        except Exception:  # noqa: BLE001
            log.exception("error closing async redis")
        _async_client = None
