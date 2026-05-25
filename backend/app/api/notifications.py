"""In-app notifications API.

GET    /api/notifications          — list (paginated, newest first)
GET    /api/notifications/unread-count
POST   /api/notifications/{id}/read
POST   /api/notifications/read-all

Today the only notification type is ``copy.retry_failed`` (a subscriber's
mirror retry exhausted on a broker-disconnect failure). The endpoints
are type-agnostic so future notification types reuse them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.database import get_db
from app.models.notification import Notification
from app.models.user import User

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(default=50, le=200),
    unread_only: bool = Query(default=False),
) -> list[dict]:
    """Most-recent first. Caller can filter to unread only via query param.

    Returns a minimal shape — id, type, message, metadata, read_at,
    created_at. Heavy lifting (joining to orders, formatting timestamps
    in local TZ) belongs on the frontend.
    """
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    q = q.order_by(Notification.created_at.desc()).limit(limit)

    rows = list(db.execute(q).scalars())
    return [
        {
            "id": str(n.id),
            "type": n.type,
            "message": n.message,
            "metadata": n.metadata_json or {},
            "read_at": n.read_at.isoformat() if n.read_at else None,
            "created_at": n.created_at.isoformat(),
        }
        for n in rows
    ]


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Powers the badge on the notification bell icon."""
    count = db.execute(
        select(func.count(Notification.id))
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    ).scalar_one()
    return {"unread": int(count)}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Mark a single notification as read. 404 if it doesn't belong to
    the caller (prevents leaking IDs across users).

    Returns {"ok": true}. (We deliberately don't use status_code=204
    here — FastAPI 0.115's APIRoute asserts that 204 endpoints have
    no response body, and a `-> None` annotation triggers that assert
    at import time. Empty 200 is more portable across versions.)
    """
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "not_found")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Mark every unread notification for the caller as read. Used by
    the inbox's "Mark all read" button.

    Returns {"ok": true, "count": <n>}.
    """
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"ok": True, "count": result.rowcount or 0}
