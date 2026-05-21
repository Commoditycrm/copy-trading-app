"""Status of the Alpaca trade_updates WebSocket listener.

A trader queries this for their own listener; a subscriber queries it for
the trader they follow. The frontend shows a small status pill that updates
both via this endpoint (on mount) and via SSE ``listener.state_changed``
events (live)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.database import get_db
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.services import trade_listener


router = APIRouter(prefix="/api/listener", tags=["listener"])


def _serialize(status: trade_listener.ListenerStatus | None) -> dict[str, Any]:
    if status is None:
        return {
            "state": "disconnected",
            "last_event_at": None,
            "state_changed_at": None,
            "last_error": None,
        }
    return {
        "state": status.state,
        "last_event_at": status.last_event_at.isoformat() if status.last_event_at else None,
        "state_changed_at": status.state_changed_at.isoformat(),
        "last_error": status.last_error,
    }


@router.get("/status")
def listener_status(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """Return the listener status the caller cares about.

    - Trader: their own listener (their Alpaca account).
    - Subscriber: the listener of the trader they follow (if any).
    """
    if user.role == UserRole.TRADER:
        return {
            "trader_id": str(user.id),
            "viewer": "trader",
            **_serialize(trade_listener.get_status(user.id)),
        }

    sub = db.get(SubscriberSettings, user.id)
    if sub is None or sub.following_trader_id is None:
        return {
            "trader_id": None,
            "viewer": "subscriber",
            "state": "no_trader",
            "last_event_at": None,
            "state_changed_at": None,
            "last_error": None,
        }
    status = trade_listener.get_status(sub.following_trader_id)
    return {
        "trader_id": str(sub.following_trader_id),
        "viewer": "subscriber",
        **_serialize(status),
    }
