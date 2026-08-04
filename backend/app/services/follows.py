"""Small query helpers for the multi-trader follow graph (``subscriber_follows``).

A subscriber can follow MANY traders at once; the join table is the source of
truth for *who mirrors whom*. Before multi-trader, code keyed off the single
``SubscriberSettings.following_trader_id`` (now just the "primary" trader). These
helpers centralize the correct membership queries so reconcilers, guards, and SSE
fanout all agree — a subscriber following a trader as a SECONDARY must be treated
identically to a primary follow.

Copy settings stay GLOBAL on ``SubscriberSettings`` — this module only answers
relationship questions, never settings.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber_follow import SubscriberFollow


def subscriber_ids_following(db: Session, trader_id: uuid.UUID) -> set[uuid.UUID]:
    """Every subscriber who follows ``trader_id`` (regardless of copy on/off).

    Use for close/flatten/roster paths that must reach ALL followers, not only
    those with copy currently enabled.
    """
    return set(
        db.execute(
            select(SubscriberFollow.subscriber_id).where(
                SubscriberFollow.trader_id == trader_id
            )
        ).scalars()
    )


def trader_ids_followed_by(db: Session, subscriber_id: uuid.UUID) -> list[uuid.UUID]:
    """Every trader ``subscriber_id`` follows."""
    return list(
        db.execute(
            select(SubscriberFollow.trader_id).where(
                SubscriberFollow.subscriber_id == subscriber_id
            )
        ).scalars()
    )


def is_following(
    db: Session, subscriber_id: uuid.UUID, trader_id: uuid.UUID
) -> bool:
    """True if ``subscriber_id`` follows ``trader_id`` (primary OR secondary)."""
    return (
        db.execute(
            select(SubscriberFollow.id).where(
                SubscriberFollow.subscriber_id == subscriber_id,
                SubscriberFollow.trader_id == trader_id,
            ).limit(1)
        ).first()
        is not None
    )
