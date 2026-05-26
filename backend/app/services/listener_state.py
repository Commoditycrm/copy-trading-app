"""Shared status surface for broker listeners.

Both the Alpaca WebSocket listener (``trade_listener.py``) and the Webull
polling listener (``webull_listener.py``) write to the same per-trader
status dict here, so:

  - ``GET /api/listener/status`` doesn't have to know which broker the
    trader is connected through; it just reads ``get_status(trader_id)``.
  - The SSE ``listener.state_changed`` event has a single source of truth,
    so the frontend pill renders the same regardless of broker.
  - Switching brokers (one-broker-per-user) cleanly transitions: the old
    listener writes ``disconnected``, the new one writes ``connecting`` →
    ``connected``.

State is in-memory per process. We deliberately don't persist it — on
restart, listeners reattach and re-publish ``connecting`` → ``connected``
on their own, which is the same flow a client gets the first time it
loads the page.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services import events

log = logging.getLogger(__name__)


# "connecting" | "connected" | "reconnecting" | "disconnected" |
# "credentials_invalid" | "mfa_required"  (webull-only)
ListenerState = str


@dataclass
class ListenerStatus:
    state: ListenerState = "connecting"
    last_event_at: datetime | None = None
    state_changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str | None = None


# One entry per trader user_id, regardless of broker. Mutated only from
# listener tasks and the start/stop helpers. Readers should snapshot
# before serialising.
_status: dict[uuid.UUID, ListenerStatus] = {}


def get_status(trader_user_id: uuid.UUID) -> ListenerStatus | None:
    return _status.get(trader_user_id)


def set_state(
    trader_user_id: uuid.UUID,
    state: ListenerState,
    *,
    error: str | None = None,
) -> None:
    """Update the listener's status snapshot and publish an SSE event so
    any interested user (the trader themselves + subscribers following
    them) sees the new state."""
    prev = _status.get(trader_user_id)
    now = datetime.now(timezone.utc)
    new = ListenerStatus(
        state=state,
        last_event_at=prev.last_event_at if prev else None,
        state_changed_at=now,
        last_error=error,
    )
    _status[trader_user_id] = new
    if not prev or prev.state != state:
        log.info("listener[%s] %s", trader_user_id, state)
        _broadcast_state_changed(trader_user_id, new)


def bump_last_event(trader_user_id: uuid.UUID) -> None:
    s = _status.get(trader_user_id)
    if s is None:
        s = ListenerStatus(state="connected")
        _status[trader_user_id] = s
    s.last_event_at = datetime.now(timezone.utc)


def clear(trader_user_id: uuid.UUID) -> None:
    """Drop the entry entirely (used on broker disconnect so the pill
    doesn't show a stale 'disconnected' for a deleted broker)."""
    _status.pop(trader_user_id, None)


def _broadcast_state_changed(trader_user_id: uuid.UUID, status: ListenerStatus) -> None:
    """Publish ``listener.state_changed`` to the trader and every
    subscriber following them. Lazy DB import keeps this module
    import-cheap (it's pulled in by lots of code paths)."""
    payload = {
        "type": "listener.state_changed",
        "trader_id": str(trader_user_id),
        "status": {
            "state": status.state,
            "last_event_at": status.last_event_at.isoformat() if status.last_event_at else None,
            "state_changed_at": status.state_changed_at.isoformat(),
            "last_error": status.last_error,
        },
    }
    # Trader sees their own listener.
    events.publish(trader_user_id, payload)
    # Subscribers following this trader also see it.
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.settings import SubscriberSettings

    with SessionLocal() as db:
        for sub_id, in db.execute(
            select(SubscriberSettings.user_id).where(
                SubscriberSettings.following_trader_id == trader_user_id
            )
        ).all():
            events.publish(sub_id, payload)
