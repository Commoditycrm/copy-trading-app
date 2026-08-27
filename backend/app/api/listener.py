"""Status of the per-trader broker listener.

A trader queries this for their own listener; a subscriber queries it for
the trader they follow. The frontend shows a small status pill that updates
both via this endpoint (on mount) and via SSE ``listener.state_changed``
events (live).

The status is broker-agnostic — Alpaca (WebSocket) and Webull (polling)
both write to the same ``listener_state`` surface, so this endpoint
doesn't need to know which broker the trader is on."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.api.deps import current_user
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.user import User, UserRole
from app.services import listener_state
from app.services.balance_sync import AUTH_ERR_PREFIX


router = APIRouter(prefix="/api/listener", tags=["listener"])


def _serialize(status: listener_state.ListenerStatus | None) -> dict[str, Any]:
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
    """Return the broker status the caller cares about.

    - Trader: their own live listener (their broker's trade stream).
    - Subscriber: their OWN broker's connection status. Subscribers have no live
      listener of their own, so this deliberately does NOT surface the trader's
      listener — a subscriber only cares whether THEIR broker is connected (so
      their mirrors can place). connection_status is only ever connected/pending;
      no account → "no_broker".
    """
    if user.role == UserRole.TRADER:
        return {
            "trader_id": str(user.id),
            "viewer": "trader",
            **_serialize(listener_state.get_status(user.id)),
        }

    accts = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
    ).scalars())
    if not accts:
        return {
            "trader_id": None,
            "viewer": "subscriber",
            "own_broker": True,
            "state": "no_broker",
            "last_event_at": None,
            "state_changed_at": None,
            "last_error": None,
        }
    # Prefer a cleanly-connected account; then any connected; then the first.
    # A connected account whose balance sync tagged an AUTH failure is reported
    # as credentials_invalid → a real "Broker offline" pill. last_error clears on
    # the next successful refresh, so the offline state self-heals.
    def _auth_failed(a: BrokerAccount) -> bool:
        return bool(a.last_error) and a.last_error.startswith(AUTH_ERR_PREFIX)

    chosen = (
        next((a for a in accts if a.connection_status == "connected" and not _auth_failed(a)), None)
        or next((a for a in accts if a.connection_status == "connected"), None)
        or accts[0]
    )
    cs = (chosen.connection_status or "").lower()
    if _auth_failed(chosen):
        state = "credentials_invalid"
    elif cs == "connected":
        state = "connected"
    elif cs == "pending":
        state = "connecting"
    else:
        state = "disconnected"
    return {
        "trader_id": None,
        "viewer": "subscriber",
        "own_broker": True,
        "state": state,
        "last_event_at": chosen.balance_updated_at.isoformat() if chosen.balance_updated_at else None,
        "state_changed_at": None,
        "last_error": chosen.last_error,
    }
