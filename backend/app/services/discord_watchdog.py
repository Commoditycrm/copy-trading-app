"""Tell the trader when Kopyya stops reading their Discord channels.

Two ways a connection fails, both caught here:

* the listener reports a channel as ``error`` or ``needs_login`` (the
  session expired, the channel is gone);
* the listener stops entirely (a crash, or a deploy that left it down — QA
  2026-10-01: every channel went "disconnected" at once and stayed down). It
  then sends nothing at all, so the only sign is heartbeats going stale.

Every minute, each trader's channels that are enabled, inside their watch
window and have connected before are checked. A channel is down when it reports
an error / needs a sign-in, or hasn't sent a heartbeat (every 30s) for
``STALE_AFTER_S``. If any are down, the trader gets ONE notification — in-app,
and SMS when "Broker connection" texts are on — naming them. They are not told
again until everything has recovered and failed afresh.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_alert_source import DiscordAlertSource
from app.models.user import User, UserRole

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 60.0
# Ten missed 30s heartbeats: long enough that a redeploy or a listener restart
# (a minute or two) doesn't text anyone, short enough to matter during a session.
STALE_AFTER_S = 300.0
_ALERTED_KEY = "discord:conn_alerted:{}"
_ALERTED_TTL_S = 7 * 24 * 3600         # a forgotten flag can't silence a user for ever
_DOWN_STATUSES = {"error", "needs_login"}
_SELF_CHANNEL_ID = "self"

_local_alerted: set[uuid.UUID] = set()   # used only when Redis is unavailable


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


def _is_alerted(user_id: uuid.UUID) -> bool:
    try:
        return bool(_redis().exists(_ALERTED_KEY.format(user_id)))
    except Exception:  # noqa: BLE001
        return user_id in _local_alerted


def _set_alerted(user_id: uuid.UUID, on: bool) -> None:
    if on:
        _local_alerted.add(user_id)
    else:
        _local_alerted.discard(user_id)
    try:
        if on:
            _redis().set(_ALERTED_KEY.format(user_id), "1", ex=_ALERTED_TTL_S)
        else:
            _redis().delete(_ALERTED_KEY.format(user_id))
    except Exception:  # noqa: BLE001
        pass


def is_down(src: DiscordAlertSource, now: datetime) -> bool:
    """Down: an error / sign-in needed, or no heartbeat for STALE_AFTER_S."""
    if (src.status or "") in _DOWN_STATUSES:
        return True
    hb = src.last_heartbeat_at
    if hb is None:
        return False                      # never connected — nothing was lost
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb).total_seconds() > STALE_AFTER_S


def _watched(src: DiscordAlertSource, now: datetime) -> bool:
    """A channel the listener is meant to be holding open right now."""
    from app.services import discord_schedule  # noqa: PLC0415

    if not src.is_enabled or src.parent_source_id is not None or src.channel_id == _SELF_CHANNEL_ID:
        return False
    if src.last_heartbeat_at is None:
        return False                      # never connected yet: not a lost connection
    # The same window the listener's assignments use: outside it the watcher is
    # closed on purpose, and that is not a lost connection.
    return discord_schedule.in_window(
        mode=src.schedule_mode, start=src.schedule_start, end=src.schedule_end,
        timezone=src.schedule_timezone, days=list(src.schedule_days or []),
    )


def _message(down: list[DiscordAlertSource]) -> str:
    names = ", ".join(s.label for s in down[:5]) + (f" and {len(down) - 5} more" if len(down) > 5 else "")
    return (
        f"Discord disconnected — Kopyya stopped reading {names}. "
        "Reconnect Discord to resume copying alerts."
    )


def check_once(db: Session, now: datetime | None = None) -> list[uuid.UUID]:
    """One pass. Returns the users notified on this pass. Caller commits."""
    from app.services.notifications import create_notification  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    by_user: dict[uuid.UUID, list[DiscordAlertSource]] = {}
    for src in db.execute(
        select(DiscordAlertSource)
        .join(User, User.id == DiscordAlertSource.user_id)
        .where(
            User.is_active.is_(True),
            User.role == UserRole.TRADER,
            User.discord_enabled.is_(True),
            DiscordAlertSource.is_enabled.is_(True),
            DiscordAlertSource.parent_source_id.is_(None),
        )
    ).scalars():
        if _watched(src, now):
            by_user.setdefault(src.user_id, []).append(src)

    notified: list[uuid.UUID] = []
    for user_id, sources in by_user.items():
        down = [s for s in sources if is_down(s, now)]
        if not down:
            if _is_alerted(user_id):
                log.info("discord-watchdog: %s reconnected", user_id)
                _set_alerted(user_id, False)
            continue
        if _is_alerted(user_id):
            continue                      # already told about this outage
        create_notification(
            db, user_id=user_id, type="discord.disconnected", message=_message(down),
            metadata={"source_ids": [str(s.id) for s in down]},
        )
        _set_alerted(user_id, True)
        notified.append(user_id)
        log.warning("discord-watchdog: %s — %d channel(s) down", user_id, len(down))
    return notified


def poll_loop(shutdown_check=None) -> None:
    from app.database import SessionLocal  # noqa: PLC0415

    log.info("discord_watchdog: starting (interval=%ss, stale after %ss)",
             POLL_INTERVAL_S, STALE_AFTER_S)
    while True:
        if shutdown_check is not None and shutdown_check():
            return
        try:
            with SessionLocal() as db:
                check_once(db)
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("discord_watchdog: pass failed")
        time.sleep(POLL_INTERVAL_S)


__all__ = ["check_once", "is_down", "poll_loop", "STALE_AFTER_S"]
