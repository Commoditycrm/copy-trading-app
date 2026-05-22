"""Redis read-through caches for fanout hot paths.

Only caches data that's safe to be ~60s stale: the subscriber-list and the
broker-account list. Decrypted credentials never leave the process — they're
held in a per-process LRU only, keyed by broker_account_id, with explicit
invalidation on credential rotation.

Failure mode: any Redis error falls through to the DB. The app must never go
down because Redis is unreachable.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.settings import SubscriberSettings
from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)


# ── plain-data DTOs (we don't want to cache SQLAlchemy instances) ─────────


@dataclass(frozen=True)
class CachedSubscriber:
    user_id: uuid.UUID
    following_trader_id: uuid.UUID
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None


@dataclass(frozen=True)
class CachedBrokerAccount:
    id: uuid.UUID
    user_id: uuid.UUID
    broker: BrokerName
    supports_fractional: bool
    connection_status: str
    encrypted_credentials: str   # opaque to the cache layer — decrypted only in-process


# ── key helpers ───────────────────────────────────────────────────────────


def _k_subs(trader_id: uuid.UUID) -> str:
    return f"cache:subs:{trader_id}"


def _k_accts(user_id: uuid.UUID) -> str:
    return f"cache:accts:{user_id}"


# ── (de)serialization ─────────────────────────────────────────────────────


def _sub_to_dict(s: SubscriberSettings | CachedSubscriber) -> dict[str, Any]:
    return {
        "user_id": str(s.user_id),
        "following_trader_id": str(s.following_trader_id) if s.following_trader_id else None,
        "copy_enabled": bool(s.copy_enabled),
        "multiplier": str(s.multiplier),
        "daily_loss_limit": str(s.daily_loss_limit) if s.daily_loss_limit is not None else None,
    }


def _sub_from_dict(d: dict[str, Any]) -> CachedSubscriber:
    return CachedSubscriber(
        user_id=uuid.UUID(d["user_id"]),
        following_trader_id=uuid.UUID(d["following_trader_id"]),
        copy_enabled=d["copy_enabled"],
        multiplier=Decimal(d["multiplier"]),
        daily_loss_limit=Decimal(d["daily_loss_limit"]) if d["daily_loss_limit"] is not None else None,
    )


def _acct_to_dict(a: BrokerAccount | CachedBrokerAccount) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "user_id": str(a.user_id),
        "broker": a.broker.value if isinstance(a.broker, BrokerName) else a.broker,
        "supports_fractional": bool(a.supports_fractional),
        "connection_status": a.connection_status,
        "encrypted_credentials": a.encrypted_credentials,
    }


def _acct_from_dict(d: dict[str, Any]) -> CachedBrokerAccount:
    return CachedBrokerAccount(
        id=uuid.UUID(d["id"]),
        user_id=uuid.UUID(d["user_id"]),
        broker=BrokerName(d["broker"]),
        supports_fractional=d["supports_fractional"],
        connection_status=d["connection_status"],
        encrypted_credentials=d["encrypted_credentials"],
    )


# ── subscribers cache ─────────────────────────────────────────────────────


async def get_subscribers_for_trader(
    db: Session, trader_id: uuid.UUID
) -> list[CachedSubscriber]:
    """Active (copy_enabled) subscribers for this trader. Cached in Redis with
    short TTL. Falls back to DB on any Redis error."""
    s = get_settings()
    r = get_async_redis()
    key = _k_subs(trader_id)
    try:
        raw = await r.get(key)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to DB", key)
        raw = None
    if raw:
        try:
            return [_sub_from_dict(d) for d in json.loads(raw)]
        except Exception:  # noqa: BLE001
            log.exception("corrupt cache entry %s — refetching", key)

    rows = (
        db.execute(
            select(SubscriberSettings).where(
                SubscriberSettings.following_trader_id == trader_id,
                SubscriberSettings.copy_enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )
    cached = [
        CachedSubscriber(
            user_id=row.user_id,
            following_trader_id=row.following_trader_id,
            copy_enabled=row.copy_enabled,
            multiplier=row.multiplier,
            daily_loss_limit=row.daily_loss_limit,
        )
        for row in rows
    ]
    try:
        await r.setex(key, s.cache_ttl_subscribers, json.dumps([_sub_to_dict(c) for c in cached]))
    except Exception:  # noqa: BLE001
        log.warning("redis setex failed for %s", key)
    return cached


def invalidate_subscribers_for_trader(trader_id: uuid.UUID) -> None:
    """Sync — call from API handlers after a subscriber subscribes / pauses /
    changes multiplier."""
    try:
        get_sync_redis().delete(_k_subs(trader_id))
    except Exception:  # noqa: BLE001
        log.warning("redis delete failed for subs:%s", trader_id)


# ── broker accounts cache ─────────────────────────────────────────────────


async def get_broker_accounts(
    db: Session, user_id: uuid.UUID
) -> list[CachedBrokerAccount]:
    s = get_settings()
    r = get_async_redis()
    key = _k_accts(user_id)
    try:
        raw = await r.get(key)
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        try:
            return [_acct_from_dict(d) for d in json.loads(raw)]
        except Exception:  # noqa: BLE001
            log.exception("corrupt cache entry %s — refetching", key)

    rows = (
        db.execute(select(BrokerAccount).where(BrokerAccount.user_id == user_id))
        .scalars()
        .all()
    )
    cached = [
        CachedBrokerAccount(
            id=row.id,
            user_id=row.user_id,
            broker=row.broker,
            supports_fractional=row.supports_fractional,
            connection_status=row.connection_status,
            encrypted_credentials=row.encrypted_credentials,
        )
        for row in rows
    ]
    try:
        await r.setex(key, s.cache_ttl_broker_accounts, json.dumps([_acct_to_dict(c) for c in cached]))
    except Exception:  # noqa: BLE001
        log.warning("redis setex failed for %s", key)
    return cached


def invalidate_broker_accounts(user_id: uuid.UUID) -> None:
    try:
        get_sync_redis().delete(_k_accts(user_id))
    except Exception:  # noqa: BLE001
        log.warning("redis delete failed for accts:%s", user_id)
    # Decrypted credentials may also need refreshing.
    _decrypted_creds_cache.cache_clear()


# ── decrypted credentials (in-process LRU only — never Redis) ─────────────


@lru_cache(maxsize=512)
def _decrypted_creds_cache(broker_account_id: str, encrypted_blob: str) -> dict:
    """Decryption is CPU-bound but cheap; the win here is skipping it 200×
    per fanout. Keyed by (id, blob) so a credential rotation naturally misses
    the cache. Held in-process only — secrets must never reach Redis."""
    from app.services.crypto import decrypt_json  # local import — avoid cycle
    return decrypt_json(encrypted_blob)


def decrypt_creds_cached(account_id: uuid.UUID, encrypted_blob: str) -> dict:
    return _decrypted_creds_cache(str(account_id), encrypted_blob)
