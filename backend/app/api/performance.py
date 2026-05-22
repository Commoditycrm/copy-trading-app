"""Fanout performance metrics for traders.

Surfaces the latency breakdown of each parent order's fanout to subscribers:
  - broker_accepted_at  : Alpaca accepted the trader's parent order
  - detected_at         : our backend created the parent Order row (for
                           API-placed orders these are ~simultaneous; for
                           externally-placed orders detected by the
                           trade_listener, this is when the WebSocket event
                           was processed)
  - fanout_completed_at : max(submitted_at) across child mirror orders — the
                           last subscriber's broker accepted their copy

Derived metrics:
  - detection_lag_ms    : detected_at - broker_accepted_at
  - fanout_duration_ms  : fanout_completed_at - detected_at
  - total_ms            : fanout_completed_at - broker_accepted_at
  - subscribers         : { total, submitted, errors }

All computed at query time from the existing orders table — no extra schema.
Trader-only.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_trader
from app.database import get_db
from app.models.order import Order, OrderStatus
from app.models.user import User

router = APIRouter(prefix="/api/performance", tags=["performance"])

# Statuses that count as "successfully submitted to broker" for the success/fail tally.
_SUCCESS_STATUSES = {
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED,
}
_ERROR_STATUSES = {OrderStatus.REJECTED}


def _ms_between(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def _serialize_child(child: Order, parent: Order, subscriber: User | None) -> dict[str, Any]:
    accepted_at = child.submitted_at
    return {
        "order_id": str(child.id),
        "subscriber_user_id": str(child.user_id),
        "subscriber_email": subscriber.email if subscriber else None,
        "subscriber_name": subscriber.display_name if subscriber else None,
        "status": child.status.value,
        "quantity": str(child.quantity),
        "filled_quantity": str(child.filled_quantity or 0),
        "broker_order_id": child.broker_order_id,
        "submitted_at": accepted_at.isoformat() if accepted_at else None,
        "created_at": child.created_at.isoformat() if child.created_at else None,
        "reject_reason": child.reject_reason,
        # Subscriber lag: from our backend detecting the parent → that
        # subscriber's broker accepting the mirror.
        "subscriber_lag_ms": _ms_between(parent.created_at, accepted_at),
    }


def _serialize_fanout(parent: Order, children: list[Order], subscribers: dict[uuid.UUID, User]) -> dict[str, Any]:
    accepted_children = [c for c in children if c.submitted_at is not None]
    last_accept_at = max((c.submitted_at for c in accepted_children), default=None)

    total = len(children)
    submitted = sum(1 for c in children if c.status in _SUCCESS_STATUSES)
    errors = sum(1 for c in children if c.status in _ERROR_STATUSES)

    detection_lag = _ms_between(parent.submitted_at, parent.created_at)
    fanout_duration = _ms_between(parent.created_at, last_accept_at)
    total_ms = _ms_between(parent.submitted_at, last_accept_at)

    return {
        "parent_order_id": str(parent.id),
        "symbol": parent.symbol,
        "side": parent.side.value,
        "quantity": str(parent.quantity),
        "instrument_type": parent.instrument_type.value,
        "broker_accepted_at": parent.submitted_at.isoformat() if parent.submitted_at else None,
        "detected_at": parent.created_at.isoformat() if parent.created_at else None,
        "fanout_completed_at": last_accept_at.isoformat() if last_accept_at else None,
        "detection_lag_ms": detection_lag,
        "fanout_duration_ms": fanout_duration,
        "total_ms": total_ms,
        "subscribers": {
            "total": total,
            "submitted": submitted,
            "errors": errors,
        },
        "children": [_serialize_child(c, parent, subscribers.get(c.user_id)) for c in children],
    }


@router.get("/fanouts")
def list_fanouts(
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
    limit: int = Query(default=25, le=200),
) -> dict[str, Any]:
    """Most recent fanouts for the calling trader, newest first.

    Returns:
        {
          "metrics": { "fanouts_shown": int, "avg_fanout_ms": int|None,
                       "max_fanout_ms": int|None, "avg_total_ms": int|None },
          "fanouts": [ {...per-fanout breakdown...} ]
        }
    """
    parents = list(
        db.execute(
            select(Order)
            .where(
                Order.user_id == trader.id,
                Order.parent_order_id.is_(None),
                Order.fanned_out_to_subscribers.is_(True),
            )
            .order_by(Order.created_at.desc())
            .limit(limit)
        ).scalars()
    )
    if not parents:
        return {
            "metrics": {
                "fanouts_shown": 0,
                "avg_fanout_ms": None,
                "max_fanout_ms": None,
                "avg_total_ms": None,
            },
            "fanouts": [],
        }

    parent_ids = [p.id for p in parents]
    children = list(
        db.execute(
            select(Order).where(Order.parent_order_id.in_(parent_ids))
        ).scalars()
    )
    children_by_parent: dict[uuid.UUID, list[Order]] = {pid: [] for pid in parent_ids}
    for c in children:
        if c.parent_order_id is not None:
            children_by_parent.setdefault(c.parent_order_id, []).append(c)

    # One round-trip for subscriber user rows (for email / display_name lookup).
    sub_ids = {c.user_id for c in children}
    subscribers: dict[uuid.UUID, User] = {}
    if sub_ids:
        rows = db.execute(select(User).where(User.id.in_(sub_ids))).scalars()
        subscribers = {u.id: u for u in rows}

    fanouts = [
        _serialize_fanout(p, children_by_parent.get(p.id, []), subscribers) for p in parents
    ]

    # Aggregate metrics — only over fanouts that actually completed (last
    # accept time present). Avoids dividing by None.
    completed_durations = [f["fanout_duration_ms"] for f in fanouts if f["fanout_duration_ms"] is not None]
    completed_totals = [f["total_ms"] for f in fanouts if f["total_ms"] is not None]

    def _avg(xs: list[int]) -> int | None:
        return int(sum(xs) / len(xs)) if xs else None

    metrics = {
        "fanouts_shown": len(fanouts),
        "avg_fanout_ms": _avg(completed_durations),
        "max_fanout_ms": max(completed_durations) if completed_durations else None,
        "avg_total_ms": _avg(completed_totals),
    }
    return {"metrics": metrics, "fanouts": fanouts}
