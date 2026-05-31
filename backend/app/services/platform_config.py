"""Runtime-overridable platform config values, stored in Redis.

Pattern: each tunable has an env-var default (in ``app.config.Settings``) and an
optional Redis override. Admin endpoints set / clear the Redis key; runtime
code calls the getter below which reads Redis first, falls back to env.

Why Redis (not the DB):
- The values are read on every fanout — DB query on the critical path adds latency
- They change rarely (admin tweaks them); Redis GET is ~0.5ms
- No migration churn for adding a new tunable

Currently exposed:
- ``fanout_batch_threshold`` — see app.services.copy_engine
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)

# Redis key namespace. Keep the prefix stable so future tunables can co-locate.
_K_FANOUT_BATCH_THRESHOLD = "config:fanout_batch_threshold"


def get_fanout_batch_threshold_sync() -> int:
    """Sync getter for callers outside an async context (admin handlers,
    initialization code). Falls back to env default if Redis is down or the
    override is missing/malformed."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = get_sync_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", _K_FANOUT_BATCH_THRESHOLD)
        return env_default
    return _coerce_threshold(raw, env_default)


async def get_fanout_batch_threshold_async() -> int:
    """Async getter used by copy_engine on the fanout hot path. Same
    fall-back semantics as the sync variant."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = await get_async_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", _K_FANOUT_BATCH_THRESHOLD)
        return env_default
    return _coerce_threshold(raw, env_default)


def set_fanout_batch_threshold(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError for non-positive ints
    so we never end up with a nonsensical 0/negative threshold."""
    r = get_sync_redis()
    if value is None:
        r.delete(_K_FANOUT_BATCH_THRESHOLD)
        return
    if value < 1:
        raise ValueError("fanout_batch_threshold must be >= 1")
    r.set(_K_FANOUT_BATCH_THRESHOLD, str(value))


def get_fanout_batch_threshold_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint — returns effective + default + whether
    an override is currently set. UI uses this to show 'Default' vs
    'Override' badges."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = get_sync_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        raw = None
    override = _coerce_optional(raw)
    return {
        "default": env_default,
        "override": override,
        "effective": override if override is not None else env_default,
    }


def _coerce_threshold(raw: object | None, fallback: int) -> int:
    """Parse a Redis value into a positive int, fall back on any error."""
    if raw is None:
        return fallback
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
        return v if v >= 1 else fallback
    except (TypeError, ValueError):
        return fallback


def _coerce_optional(raw: object | None) -> int | None:
    """Parse to int or return None if missing/malformed. Used by the GET
    endpoint to surface 'override is unset' truthfully."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
        return v if v >= 1 else None
    except (TypeError, ValueError):
        return None
