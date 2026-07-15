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
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.user import User
from app.services import events

log = logging.getLogger(__name__)

RETENTION_DAYS = 30

# Notification type → in-app path appended to the SMS body (NOT the in-app
# message, which stays clean) so a tapped text drops the user on the right
# screen. Types not listed get no link.
_SMS_DEEP_LINK = {
    "copy.rejected": "/trades",
    "order.rejected": "/trades",
    "copy.auto_liquidated": "/positions",
    "broker.disconnected": "/broker",
}

# Notification type → the User column that gates its SMS.
#
# A type absent from BOTH maps below NEVER sends SMS — it stays in-app only.
# That's deliberate: our A2P 10DLC campaign is registered with sample messages
# covering exactly these three categories, and carriers audit live traffic
# against the samples on file. Texting a follow request — which no sample
# covers — is how a campaign gets flagged. Adding a category here means filing
# a new sample message with Twilio first.
_SMS_PREF_EXACT = {
    "copy.auto_liquidated": "sms_on_auto_actions",
    "copy.auto_resumed_next_day": "sms_on_auto_actions",
    "order.rejected": "sms_on_trade_rejected",
    "copy.rejected": "sms_on_trade_rejected",
    "trader.order_rejected": "sms_on_trade_rejected",
    "broker.disconnected": "sms_on_broker_connection",
}

# These types are built with f-strings (e.g. f"copy.auto_paused_{reason}"), so
# exact lookup can't match them — the suffix is runtime data.
_SMS_PREF_PREFIX = (
    ("copy.auto_paused_", "sms_on_auto_actions"),
    ("position.auto_closed_", "sms_on_auto_actions"),
)


def _sms_pref_attr(notif_type: str) -> str | None:
    """The User flag gating SMS for this notification type, or None when the
    type is in-app only."""
    attr = _SMS_PREF_EXACT.get(notif_type)
    if attr is not None:
        return attr
    for prefix, prefix_attr in _SMS_PREF_PREFIX:
        if notif_type.startswith(prefix):
            return prefix_attr
    return None


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

    # Opt-in SMS fanout: mirror the notification to Twilio for users who gave a
    # phone and enabled SMS. Fire off-thread so a slow/failing send never blocks
    # the caller — send_sms reads only config + httpx (no DB/session), so it's
    # safe outside this request's session. Best-effort; never raises upward.
    try:
        pref_attr = _sms_pref_attr(type)
        user = db.get(User, user_id) if pref_attr else None
        if (
            user is not None
            and user.phone
            and user.sms_notifications_enabled      # master switch
            and getattr(user, pref_attr)            # this category
        ):
            from app.services.sms import send_sms  # noqa: PLC0415
            # Append a deep link for SMS only (keeps the in-app message clean),
            # so tapping the text opens the relevant screen.
            sms_body = message
            path = _SMS_DEEP_LINK.get(type)
            if path:
                from app.config import get_settings  # noqa: PLC0415
                base = get_settings().frontend_base_url.rstrip("/")
                sms_body = f"{message} View: {base}{path}"
            threading.Thread(
                target=send_sms, args=(user.phone, sms_body), daemon=True,
            ).start()
    except Exception:  # noqa: BLE001
        log.exception("notifications: SMS fanout failed for user=%s", user_id)

    log.info(
        "notifications: created user=%s type=%s id=%s",
        user_id, type, notif.id,
    )
    return notif
