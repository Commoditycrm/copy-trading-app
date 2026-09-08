"""Auto-cancel a subscriber's copied order that stays WORKING (unfilled) too long.

When a trade fans out, a subscriber's mirror can sit unfilled (a limit that never
fills, an illiquid book). If the subscriber has opted in
(``SubscriberSettings.unfilled_timeout_enabled``), this scanner cancels the mirror
at the broker once it has been working longer than their
``unfilled_timeout_seconds``, marks it CANCELED, and notifies them.

Scope (per product decision): copied mirrors — entries AND closes
(``parent_order_id IS NOT NULL``) — restricted to MARKET / LIMIT orders, the ones
meant to fill promptly. STOP / STOP_LIMIT / TRAILING_STOP rest until their trigger
by design, so they're excluded (cancelling them would defeat a copied stop entry
or strip a protective stop); emulated bracket TP/SL exit legs are excluded too
(they carry no ``parent_order_id``, plus a ``bracket_leg IS NULL`` guard). For a
cancelled CLOSE the notification warns the subscriber they may still hold the
position.

Distinct from ``retry_scheduler`` (which re-places orders the broker REJECTED —
status RETRY_PENDING, which is NOT a working status, so the two never overlap).
Modeled on ``retry_scheduler``: a sync daemon thread, select IDs in one session,
process each in its own fresh session with a status re-check for idempotency.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.brokers import adapter_for
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.notification import Notification  # noqa: F401 — ORM registration
from app.models.order import Order, OrderStatus, OrderType
from app.models.settings import SubscriberSettings
from app.services import audit, events
from app.services.copy_engine import _order_event, _WORKING_ORDER_STATUSES
from app.services.crypto import decrypt_json
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

BATCH_SIZE = 50


def _fmt_duration(seconds: int) -> str:
    """Human-friendly duration for messages: 45 → '45s', 300 → '5m', 5400 → '90m'."""
    seconds = int(seconds)
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _due_order_ids() -> list[uuid.UUID]:
    """Working copied mirrors whose age exceeds THEIR subscriber's timeout.

    The per-subscriber timeout lives in the join, so one query covers everyone:
    ``coalesce(submitted_at, created_at) <= now() - make_interval(secs => timeout)``.
    """
    cutoff = func.coalesce(Order.submitted_at, Order.created_at)
    threshold = func.now() - func.make_interval(
        0, 0, 0, 0, 0, 0, SubscriberSettings.unfilled_timeout_seconds
    )
    with SessionLocal() as db:
        return list(
            db.execute(
                select(Order.id)
                .join(SubscriberSettings, SubscriberSettings.user_id == Order.user_id)
                .where(
                    Order.parent_order_id.isnot(None),      # copied mirrors only
                    Order.broker_account_id.isnot(None),     # broker still connected
                    Order.broker_order_id.isnot(None),       # has a live broker order
                    Order.status.in_(_WORKING_ORDER_STATUSES),
                    # Only orders MEANT to fill promptly. STOP / STOP_LIMIT /
                    # TRAILING_STOP rest until their trigger by design — cancelling
                    # them on a fill-timeout would defeat a copied stop entry or
                    # strip a protective stop. bracket_leg guards against any
                    # emulated TP/SL exit that ever carries a parent_order_id.
                    Order.order_type.in_((OrderType.MARKET, OrderType.LIMIT)),
                    Order.bracket_leg.is_(None),
                    SubscriberSettings.unfilled_timeout_enabled.is_(True),
                    cutoff <= threshold,
                )
                .order_by(cutoff.asc())
                .limit(BATCH_SIZE)
            ).scalars()
        )


def _cancel_one(order_id: uuid.UUID) -> str:
    """Cancel one stale mirror in its own session. Returns a short outcome string.

    Idempotent: re-checks the order is still working and still due before acting,
    so a fill / manual cancel / setting change between select and process is a
    no-op. Only a genuinely-cancelled order (broker returned True) is marked
    CANCELED and notified — a broker refusal is left for the next tick, so we
    never spam notifications.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        o = db.get(Order, order_id)
        if o is None or o.status not in _WORKING_ORDER_STATUSES:
            return "vanished"

        s = db.get(SubscriberSettings, o.user_id)
        if s is None or not s.unfilled_timeout_enabled:
            return "not_enabled"

        started = o.submitted_at or o.created_at
        if started is None:
            return "no_start_ts"
        elapsed = (now - started).total_seconds()
        if elapsed < s.unfilled_timeout_seconds:
            return "not_due"  # setting was raised after selection

        acct = db.get(BrokerAccount, o.broker_account_id)
        if acct is None:
            return "no_account"  # orphaned broker — nothing to cancel at
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))

        human = _fmt_duration(int(s.unfilled_timeout_seconds))
        try:
            cancelled = adapter.cancel_order(o.broker_order_id)
        except Exception as exc:  # noqa: BLE001 — broker error: leave for next tick
            log.warning(
                "stale_order_canceller: broker cancel failed order=%s: %s", o.id, exc
            )
            audit.record(
                db, actor_user_id=o.user_id,
                action="order.auto_cancel_failed",
                entity_type="order", entity_id=o.id,
                metadata={"error": str(exc)[:300], "broker_order_id": o.broker_order_id},
            )
            db.commit()
            return "broker_error"

        if not cancelled:
            # Already terminal at the broker (likely just filled) — don't touch
            # local state; fills_sync will reconcile the true status.
            return "already_terminal"

        o.status = OrderStatus.CANCELED
        o.closed_at = now
        o.reject_reason = f"Auto-cancelled: unfilled after {human}"
        audit.record(
            db, actor_user_id=o.user_id,
            action="order.auto_cancelled_unfilled",
            entity_type="order", entity_id=o.id,
            metadata={
                "symbol": o.symbol,
                "side": o.side.value,
                "is_closing": bool(o.is_closing),
                "parent_order_id": str(o.parent_order_id) if o.parent_order_id else None,
                "timeout_seconds": int(s.unfilled_timeout_seconds),
                "elapsed_seconds": int(elapsed),
            },
        )
        db.commit()

        # SSE: reuse the existing order.cancelled event so the Trades UI updates
        # live with no new client wiring.
        try:
            events.publish(o.user_id, _order_event("order.cancelled", o))
        except Exception:  # noqa: BLE001
            log.warning("stale_order_canceller: event publish failed order=%s", o.id)

        # In-app notification (type is absent from the SMS maps → in-app only).
        msg = (
            f"Your copied {o.side.value.upper()} order for {o.symbol} was "
            f"automatically cancelled — it stayed unfilled for over {human}."
        )
        if o.is_closing:
            msg += (
                " This was a closing order, so you may still hold the position — "
                "please review and manage it in your account."
            )
        create_notification(
            db, user_id=o.user_id,
            type="copy.order_auto_cancelled_unfilled",
            message=msg,
            metadata={
                "order_id": str(o.id),
                "parent_order_id": str(o.parent_order_id) if o.parent_order_id else None,
                "symbol": o.symbol,
                "side": o.side.value,
                "is_closing": bool(o.is_closing),
                "timeout_seconds": int(s.unfilled_timeout_seconds),
            },
        )
        db.commit()
        return "cancelled"


def _tick() -> None:
    """One scan pass: cancel every currently-due stale mirror."""
    for order_id in _due_order_ids():
        try:
            outcome = _cancel_one(order_id)
            if outcome not in ("vanished", "not_due", "not_enabled"):
                log.info("stale_order_canceller: order=%s outcome=%s", order_id, outcome)
        except Exception:  # noqa: BLE001
            log.exception("stale_order_canceller: error on order=%s", order_id)


def poll_loop(shutdown_check=None) -> None:
    """Long-running loop. Every ``unfilled_order_scan_interval_s`` seconds, cancels
    subscriber mirrors that have been working longer than their per-account
    timeout. Runs only in the background-worker process."""
    interval = get_settings().unfilled_order_scan_interval_s
    log.info("stale_order_canceller: starting (interval=%ss, batch=%d)", interval, BATCH_SIZE)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("stale_order_canceller: shutdown requested, exiting")
            return
        try:
            _tick()
        except Exception:  # noqa: BLE001
            log.exception("stale_order_canceller: poll iteration failed")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("stale_order_canceller: KeyboardInterrupt, exiting")
            return
