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
_K_ALPACA_PNL_POLL_INTERVAL = "config:alpaca_pnl_poll_interval_s"

# Inclusive bounds on the Alpaca poll interval. 1s is the floor — below
# that and we're spending more on outer-loop coordination than on real
# broker work. Even at 1s you only burn 1-2 req/sec/account against
# Alpaca's 3.3 req/sec/account budget, so quota-wise it's safe.
# 300s = 5 min is the ceiling so kill-switch latency never gets
# shockingly large.
_ALPACA_PNL_POLL_MIN_S = 1
_ALPACA_PNL_POLL_MAX_S = 300


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


# ── Alpaca P&L poll interval ────────────────────────────────────────────
# Same env-default + Redis-override pattern as fanout_batch_threshold.
# pnl_poller reads this on each tick (see _alpaca_interval_s) so an
# admin change takes effect on the very next tick — no restart needed.


def get_alpaca_pnl_poll_interval_sync() -> int:
    """Sync getter used by pnl_poller and the admin GET endpoint.
    Always returns a value inside [_ALPACA_PNL_POLL_MIN_S,
    _ALPACA_PNL_POLL_MAX_S]; out-of-range or malformed Redis values
    fall back to env default."""
    env_default = get_settings().alpaca_pnl_poll_interval_s
    try:
        raw = get_sync_redis().get(_K_ALPACA_PNL_POLL_INTERVAL)
    except Exception:  # noqa: BLE001
        log.warning(
            "redis get failed for %s — falling back to env default",
            _K_ALPACA_PNL_POLL_INTERVAL,
        )
        return _clamp_alpaca_interval(env_default, env_default)
    return _clamp_alpaca_interval(raw, env_default)


def set_alpaca_pnl_poll_interval(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError if outside the
    [5, 300] bound — protects against a misclicked extra zero pinning
    the poller at hours-per-tick."""
    r = get_sync_redis()
    if value is None:
        r.delete(_K_ALPACA_PNL_POLL_INTERVAL)
        return
    if value < _ALPACA_PNL_POLL_MIN_S or value > _ALPACA_PNL_POLL_MAX_S:
        raise ValueError(
            f"alpaca_pnl_poll_interval_s must be between "
            f"{_ALPACA_PNL_POLL_MIN_S} and {_ALPACA_PNL_POLL_MAX_S}"
        )
    r.set(_K_ALPACA_PNL_POLL_INTERVAL, str(value))


def get_alpaca_pnl_poll_interval_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint. ``override`` is None when no Redis
    key is set OR when the stored value is out-of-bounds (in which case
    the poller is silently using the env default and the UI should
    reflect that)."""
    env_default = get_settings().alpaca_pnl_poll_interval_s
    try:
        raw = get_sync_redis().get(_K_ALPACA_PNL_POLL_INTERVAL)
    except Exception:  # noqa: BLE001
        raw = None
    override = _coerce_optional_in_range(
        raw, _ALPACA_PNL_POLL_MIN_S, _ALPACA_PNL_POLL_MAX_S,
    )
    return {
        "default": env_default,
        "override": override,
        "effective": override if override is not None else env_default,
        "min": _ALPACA_PNL_POLL_MIN_S,
        "max": _ALPACA_PNL_POLL_MAX_S,
    }


def _clamp_alpaca_interval(raw: object | None, fallback: int) -> int:
    """Like _coerce_threshold but also clamps to [min, max]. The poller
    is the hot-path caller — we don't want to keep hitting Redis with a
    parsed-but-invalid value, so we silently clamp instead of erroring."""
    if raw is None:
        return fallback
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    if v < _ALPACA_PNL_POLL_MIN_S or v > _ALPACA_PNL_POLL_MAX_S:
        return fallback
    return v


def _coerce_optional_in_range(
    raw: object | None, lo: int, hi: int,
) -> int | None:
    """Same shape as _coerce_optional but rejects out-of-bound values
    so the admin UI's 'Override: -' renders truthfully."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None
