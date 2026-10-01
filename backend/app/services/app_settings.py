"""Read/write global runtime flags (app_settings table).

``flag(key, default)`` is the read path used by the market-stream supervisors:
it returns the DB override when a row exists, else the env-provided default. A
short in-process TTL cache keeps the supervisors' frequent polls off the DB;
``set_flag`` writes through and busts the cache so an admin toggle takes effect
within one supervisor pass. Tolerant of a missing table (returns the default) so
it can't break startup before the migration runs.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.app_setting import AppSetting

_CACHE_TTL_S = 10.0
_cache: dict[str, tuple[float, str | None]] = {}  # key -> (expires_at, raw value|None)

_TRUE = {"1", "true", "t", "yes", "on"}
_FALSE = {"0", "false", "f", "no", "off"}


def _read_raw(key: str) -> str | None:
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]
    raw: str | None = None
    try:
        with SessionLocal() as db:
            raw = db.execute(
                select(AppSetting.value).where(AppSetting.key == key)
            ).scalar_one_or_none()
    except SQLAlchemyError:
        raw = None  # table not migrated yet / transient — fall back to default
    _cache[key] = (now + _CACHE_TTL_S, raw)
    return raw


def flag(key: str, default: bool) -> bool:
    """The effective boolean for ``key``: the DB override if set, else ``default``
    (the env value). Cached for a few seconds."""
    raw = _read_raw(key)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def get_override(db: Session, key: str) -> bool | None:
    """The stored override for ``key``, or None when no row exists (using the
    env default). For the admin read endpoint."""
    raw = db.execute(
        select(AppSetting.value).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def set_flag(db: Session, key: str, value: bool) -> None:
    """Upsert the override and bust the cache so supervisors pick it up promptly.
    Caller commits."""
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value="true" if value else "false"))
    else:
        row.value = "true" if value else "false"
    _cache.pop(key, None)
