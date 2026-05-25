"""In-app notification helpers.

Persistent so the subscriber can read them at next login even if their
browser was closed when the underlying event happened. Push via SSE too
so users with the app open see them appear in real time.

Retention
---------
30-day inline cleanup: every call to ``create_notification`` (after
inserting the new row) opportunistically deletes notifications older
than 30 days for the same user. Spreads the cleanup work across calls
instead of needing a cron job — Render free tier doesn't support
scheduled tasks natively.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.services import events

log = logging.getLogger(__name__)

RETENTION_DAYS = 30


def create_notification(
    db: Session,
    *,
    user_id: uuid.UUID,
    type: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> Notification:
    """Insert a notification row, publish an SSE event so the user's open
    tab(s) see it immediately, and opportunistically delete any of this
    user's notifications older than RETENTION_DAYS.

    Caller is responsible for committing the session (typical pattern in
    this codebase — services accept a session, the route commits).
    """
    notif = Notification(
        user_id=user_id,
        type=type,
        message=message,
        metadata_json=metadata,
        created_at=datetime.now(timezone.utc),
    )
    db.add(notif)
    db.flush()

    # Opportunistic cleanup: delete this user's notifications older than
    # the retention window. Doing it per-create distributes the work and
    # avoids a cron job. The DELETE is bounded by user_id + created_at
    # index lookups so it's cheap (no full table scan).
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    try:
        db.execute(
            delete(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.created_at < cutoff,
            )
        )
    except Exception:  # noqa: BLE001
        # Cleanup failure must NOT prevent the notification itself from
        # being recorded. Log and move on.
        log.exception("notifications: retention cleanup failed for user=%s", user_id)

    # Real-time push via the SSE bus (Redis pub/sub on this branch — same
    # publish(user_id, dict) signature as the in-process bus). The
    # subscriber's open browser tab (if any) gets the toast / bell-badge
    # update without needing to poll.
    events.publish(user_id, {
        "type": "notification.created",
        "notification": {
            "id": str(notif.id),
            "type": notif.type,
            "message": notif.message,
            "metadata": notif.metadata_json or {},
            "created_at": notif.created_at.isoformat(),
        },
    })

    log.info(
        "notifications: created user=%s type=%s id=%s",
        user_id, type, notif.id,
    )
    return notif
