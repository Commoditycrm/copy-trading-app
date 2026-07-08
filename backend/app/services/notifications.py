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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.order import Order
from app.models.user import User
from app.services import events
from app.services.email import send_notification_email
from app.services.sms import send_sms

log = logging.getLogger(__name__)

RETENTION_DAYS = 30

# Off-request dispatch pool for the email/SMS legs. A trade must never block
# on SendGrid/Twilio, and these events (fills/rejects) are low-rate, so a
# small shared pool is plenty.
_dispatch_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="notify")

# Human heading per event — used for the email card + default subject.
_HEADINGS = {
    "order.filled":   "Order filled",
    "order.rejected": "Order rejected",
}


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


def get_or_create_prefs(db: Session, user_id: uuid.UUID) -> NotificationPreference:
    """The user's notification preferences, creating a defaults row on first
    access (email on, sms off). Caller commits."""
    pref = db.get(NotificationPreference, user_id)
    if pref is None:
        pref = NotificationPreference(user_id=user_id)
        db.add(pref)
        db.flush()
    return pref


def _safe_send_email(to: str, subject: str, heading: str, message: str) -> None:
    try:
        send_notification_email(to, subject, heading, message)
    except Exception:  # noqa: BLE001
        log.exception("notify: email leg failed to=%s", to)


def _safe_send_sms(to: str, body: str) -> None:
    try:
        send_sms(to, body)
    except Exception:  # noqa: BLE001
        log.exception("notify: sms leg failed to=%s", to)


def notify_user(
    db: Session,
    *,
    user: User,
    event_type: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    subject: str | None = None,
    sms_body: str | None = None,
) -> None:
    """Deliver one event to a user across every channel they've enabled.

    Always writes the in-app notification (persistent inbox + SSE). Then, per
    the user's NotificationPreference, fires the email and/or SMS legs on a
    background pool so the caller (copy_engine fanout, fill reconciler) never
    blocks on SendGrid/Twilio. SMS additionally requires a verified phone.

    Caller owns the commit — the in-app row + any auto-created prefs row are
    flushed here and committed with the caller's transaction.
    """
    create_notification(
        db, user_id=user.id, type=event_type, message=message, metadata=metadata,
    )
    prefs = get_or_create_prefs(db, user.id)

    # Decide + capture primitives on THIS thread (never touch the ORM/session
    # from the pool threads), then hand the plain strings off to be sent.
    heading = _HEADINGS.get(event_type, "Notification")
    subj = subject or f"{get_settings().email_from_name}: {heading}"
    to_email = user.email
    to_phone = user.phone_number

    if to_email and prefs.channel_enabled(event_type, "email"):
        _dispatch_pool.submit(_safe_send_email, to_email, subj, heading, message)

    if (
        to_phone
        and user.phone_verified
        and prefs.channel_enabled(event_type, "sms")
    ):
        _dispatch_pool.submit(_safe_send_sms, to_phone, sms_body or message)


def notify_order_event(db: Session, order: Order, event_type: str) -> None:
    """Convenience wrapper: build a filled/rejected message from an order row
    and notify its owner. Safe to call from reconcilers / the fanout loop —
    loads the owning user and no-ops on anything unexpected."""
    if event_type not in ("order.filled", "order.rejected"):
        return
    user = db.get(User, order.user_id)
    if user is None:
        return
    sym = order.symbol
    side = order.side.value.upper() if order.side else ""
    qty = str(order.quantity)
    if event_type == "order.filled":
        message = f"Your {side} {sym} ×{qty} order filled."
    else:
        reason = order.reject_reason or "rejected by broker"
        message = f"Your {side} {sym} ×{qty} order was rejected: {reason}"
    try:
        notify_user(
            db, user=user, event_type=event_type, message=message,
            metadata={
                "order_id": str(order.id),
                "symbol": sym,
                "side": order.side.value if order.side else None,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("notify_order_event failed order=%s event=%s", order.id, event_type)
