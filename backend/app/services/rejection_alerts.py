"""Tell the owner when the broker rejects an order it had already accepted.

A rejection AT PLACEMENT is reported where it happens: the broker refuses the
request, and ``_place_trader_order`` / the copy engine raise "Your … order was
rejected" (in-app, and by text for those who turned texts on). But a broker
can also accept an order and reject it a moment later — the order then reaches
REJECTED through the order stream, the order poll or the fill sync, which only
update its status. Nobody was told, and nothing was texted.

One hook catches every such change, wherever it is written: an order going
from accepted (SUBMITTED / ACCEPTED / PARTIALLY_FILLED) to REJECTED. A
placement rejection goes from PENDING and is reported where it happens, so it
is not reported twice.

The notification is the existing "order.rejected" (or "copy.rejected" for a
subscriber's copy) — the category texts are registered for, sent by
services/notifications.py to the phone in Settings when texts and "Trade
rejected" are on.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.order import Order, OrderStatus

log = logging.getLogger(__name__)

_ACCEPTED = (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
_KEY = "rejection_alerts.pending"
_installed = False


def _was_accepted(session: Session, order: Order) -> bool:
    """Is this order changing from accepted to REJECTED in this flush?"""
    if order.status != OrderStatus.REJECTED:
        return False
    hist = inspect(order).attrs.status.history
    if not hist.has_changes():
        return False
    if hist.deleted:
        return hist.deleted[0] in _ACCEPTED
    # Set on a row whose loaded values had expired (after a commit): the
    # previous status isn't in memory, so read what the database still holds.
    from sqlalchemy import select  # noqa: PLC0415

    with session.no_autoflush:
        before = session.execute(select(Order.status).where(Order.id == order.id)).scalar_one_or_none()
    return before in _ACCEPTED


def _before_flush(session: Session, flush_context, instances) -> None:  # noqa: ANN001, ARG001
    try:
        for obj in list(session.dirty):
            if isinstance(obj, Order) and _was_accepted(session, obj):
                session.info.setdefault(_KEY, set()).add(obj.id)
    except Exception:  # noqa: BLE001 — a notice is never worth failing a flush for
        log.exception("rejection alerts: could not check an order")


def _after_commit(session: Session) -> None:
    ids = session.info.pop(_KEY, None)
    if ids:
        notify(ids)


def _after_rollback(session: Session) -> None:
    session.info.pop(_KEY, None)


def message_for(order: Order) -> tuple[str, str]:
    """(notification type, message) for an order the broker rejected after
    accepting it."""
    from app.services.notifications import instrument_label  # noqa: PLC0415

    reason = (order.reject_reason or "").strip()
    side = order.side.value.upper() if order.side else ""
    what = f"{side} {instrument_label(order)}".strip()
    tail = f": {reason[:180]}" if reason else "."
    if order.parent_order_id is not None:
        return "copy.rejected", f"Your copy of the {what} order was rejected by your broker{tail}"
    return "order.rejected", f"Your {what} order was rejected by your broker{tail}"


def notify(order_ids) -> None:
    """Create the notice for each order, in its own session (after the commit
    that recorded the rejection). Never raises."""
    from app.database import SessionLocal  # noqa: PLC0415
    from app.services import notifications as notif_svc  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            for oid in order_ids:
                order = db.get(Order, oid if isinstance(oid, uuid.UUID) else uuid.UUID(str(oid)))
                if order is None or order.status != OrderStatus.REJECTED or order.user_id is None:
                    continue
                kind, message = message_for(order)
                notif_svc.create_notification(
                    db, user_id=order.user_id, type=kind, message=message,
                    metadata={
                        "order_id": str(order.id),
                        "symbol": order.symbol,
                        "instrument": notif_svc.instrument_label(order),
                        "side": order.side.value if order.side else None,
                        "reason": (order.reject_reason or "")[:300],
                        "after_accepted": True,
                    },
                )
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("rejection alerts: could not notify for %s", list(order_ids))


def install() -> None:
    """Start watching. Idempotent."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _installed = True


__all__ = ["install", "message_for", "notify"]
