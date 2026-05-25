"""Background scheduler that retries failed subscriber mirror orders.

Picks up Order rows where status=RETRY_PENDING AND retry_at <= now()
AND retry_attempted=false. Re-runs the gate checks (subscriber's
copy_enabled, daily_loss_limit, trader's master switch — same checks
that ran the first time). If they all pass, attempts the broker call
again via place_order_with_recovery.

On success → status=SUBMITTED, broker_order_id filled in, audit
``copy.retry_succeeded``, SSE event so subscriber UI updates.

On failure → status=REJECTED, audit ``copy.retry_failed``, and a
persistent Notification is created for the subscriber (so they see
it next time they log in even if their browser was closed during
the retry window).

Single-retry policy
-------------------
v1 = ONE retry attempt per failed order. After that, retry_attempted
is set True and we never try again. This keeps the state machine
simple and predictable for the demo. If the client wants N retries
spaced N minutes apart, easy upgrade later (just add a retries_left
counter and decrement instead of flipping the bool).

Single-process design
---------------------
Runs as one background thread inside the FastAPI process (same model
as the rest of this codebase). If you eventually run a dedicated
worker service, also start this loop there — the work happens
directly off the DB so there are no queue semantics to coordinate.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.notification import Notification  # noqa: F401  — ORM registration
from app.models.order import Order, OrderStatus
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User
from app.services import audit, events
from app.services.copy_engine import _order_event
from app.services.crypto import decrypt_json
from app.services.notifications import create_notification
from app.services.order_retry import RecoverableOrderError, place_order_with_recovery
from app.services.pnl import today_realized_pnl

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
BATCH_SIZE = 50


# ── helpers ────────────────────────────────────────────────────────────────

def _trader_email(db: Session, trader_id: uuid.UUID) -> str:
    """Best-effort display string for the trader in a notification message."""
    u = db.get(User, trader_id)
    if u is None:
        return "unknown trader"
    return u.display_name or u.email


def _notify_retry_failed(
    db: Session,
    child: Order,
    trader_order: Order,
    reason: str,
) -> None:
    """Drop a persistent notification on the subscriber telling them
    their mirror retry didn't make it. Wording is intentionally short
    and explicit about WHY ("broker unreachable") so they don't think
    the platform itself is broken."""
    trader_name = _trader_email(db, trader_order.user_id)
    symbol = trader_order.symbol
    side = trader_order.side.value.upper()
    qty = str(child.quantity)
    instrument = "option" if trader_order.instrument_type.value == "option" else "share"

    message = (
        f"Your mirror of {trader_name}'s {side} {qty} {symbol} "
        f"{instrument} order failed after 1 retry "
        f"(broker was unreachable). Reason: {reason[:200]}"
    )
    create_notification(
        db,
        user_id=child.user_id,
        type="copy.retry_failed",
        message=message,
        metadata={
            "child_order_id": str(child.id),
            "parent_order_id": str(trader_order.id),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "reason": reason[:300],
            "trader_id": str(trader_order.user_id),
            "trader_name": trader_name,
        },
    )


def _passes_gates(db: Session, child: Order, trader_order: Order) -> str | None:
    """Re-check the same gates that ran on the original attempt. Returns
    None if all pass, else a short reason string (which we put on the
    REJECTED row + notification).

    These checks all use the FRESH database state at retry time, so a
    subscriber who disabled copy or hit their daily loss limit between
    the original attempt and the retry will see the retry skipped
    correctly."""
    # Subscriber settings (copy_enabled, daily_loss_limit, retry interval)
    sub_settings = db.get(SubscriberSettings, child.user_id)
    if sub_settings is None or not sub_settings.copy_enabled:
        return "copy_disabled"
    if sub_settings.following_trader_id != trader_order.user_id:
        return "no_longer_following"

    # Subscriber may have changed retry_interval to "never" while the
    # order was pending. Respect that: skip the retry.
    interval = (
        sub_settings.retry_interval_close if child.is_closing
        else sub_settings.retry_interval_open
    )
    if interval == RetryInterval.NEVER:
        return "retry_disabled_by_subscriber"

    # Daily-loss kill switch (same check as in copy_engine).
    if sub_settings.daily_loss_limit is not None:
        todays_pnl = today_realized_pnl(db, child.user_id)
        if todays_pnl <= -sub_settings.daily_loss_limit:
            sub_settings.copy_enabled = False
            return "daily_loss_limit_hit"

    # Trader master switches
    ts = db.get(TraderSettings, trader_order.user_id)
    if ts is None or not ts.trading_enabled:
        return "trader_master_off"
    if ts.copy_paused:
        return "trader_paused_copy"
    return None


# ── per-order retry ─────────────────────────────────────────────────────────

def _retry_one_order(order_id: uuid.UUID) -> str:
    """Pick up one RETRY_PENDING order, run gates + broker call. Returns
    a short outcome string for logging: "succeeded" / "gate_failed:<reason>"
    / "broker_failed" / "vanished"."""
    with SessionLocal() as db:
        child = db.get(Order, order_id)
        if child is None or child.status != OrderStatus.RETRY_PENDING:
            return "vanished"
        if child.retry_attempted:
            # Defence in depth — shouldn't happen given our query, but if
            # two scheduler threads pick the same row, the second one bails.
            return "already_attempted"

        trader_order = db.get(Order, child.parent_order_id) if child.parent_order_id else None
        if trader_order is None:
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = "parent_order_missing"
            child.closed_at = datetime.now(timezone.utc)
            db.commit()
            return "vanished"

        # Re-run the gates on FRESH state
        gate_skip = _passes_gates(db, child, trader_order)
        if gate_skip:
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = f"retry_skipped: {gate_skip}"
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db,
                actor_user_id=child.user_id,
                action="copy.retry_skipped",
                entity_type="order",
                entity_id=child.id,
                metadata={"reason": gate_skip, "parent_order_id": str(trader_order.id)},
            )
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_failed", child))
            return f"gate_failed:{gate_skip}"

        # Load broker account + creds
        acct = db.get(BrokerAccount, child.broker_account_id)
        if acct is None:
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = "broker_account_missing"
            child.closed_at = datetime.now(timezone.utc)
            db.commit()
            return "vanished"

        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
        except Exception as exc:  # noqa: BLE001
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = f"credentials_error: {exc}"[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_failed",
                entity_type="order", entity_id=child.id,
                metadata={"reason": "credentials_error", "error": str(exc)[:300]},
            )
            _notify_retry_failed(db, child, trader_order, f"Broker credentials error: {exc}")
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_failed", child))
            return "broker_failed"

        request = BrokerOrderRequest(
            instrument_type=child.instrument_type,
            symbol=child.symbol,
            side=child.side,
            order_type=child.order_type,
            quantity=child.quantity,
            limit_price=child.limit_price,
            stop_price=child.stop_price,
            option_expiry=child.option_expiry,
            option_strike=child.option_strike,
            option_right=child.option_right,
            client_order_id=str(child.id),
        )

        # Single broker call — succeed, fail, or surface a clean reason.
        try:
            resp = place_order_with_recovery(adapter, request)
        except RecoverableOrderError as rec:
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = rec.friendly_message[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_failed",
                entity_type="order", entity_id=child.id,
                metadata={
                    "friendly": rec.friendly_message,
                    "raw": str(rec.original)[:300],
                    "classification": "user_fixable_on_retry",
                },
            )
            _notify_retry_failed(db, child, trader_order, rec.friendly_message)
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_failed", child))
            return "broker_failed"
        except Exception as exc:  # noqa: BLE001
            child.status = OrderStatus.REJECTED
            child.retry_attempted = True
            child.reject_reason = str(exc)[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_failed",
                entity_type="order", entity_id=child.id,
                metadata={"error": str(exc)[:480], "classification": "still_transient_or_unknown"},
            )
            _notify_retry_failed(db, child, trader_order, str(exc))
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_failed", child))
            return "broker_failed"

        # Success on retry
        child.status = resp.status
        child.broker_order_id = resp.broker_order_id
        child.submitted_at = resp.submitted_at
        child.filled_quantity = resp.filled_quantity
        child.filled_avg_price = resp.filled_avg_price
        child.retry_attempted = True
        child.reject_reason = None
        audit.record(
            db, actor_user_id=child.user_id, action="copy.retry_succeeded",
            entity_type="order", entity_id=child.id,
            metadata={
                "parent_order_id": str(trader_order.id),
                "broker_order_id": resp.broker_order_id,
            },
        )
        db.commit()
        events.publish(child.user_id, _order_event("order.copy_submitted", child))
        return "succeeded"


# ── scheduler loop ─────────────────────────────────────────────────────────

_LAST_HEARTBEAT: dict[str, Any] = {"at": None}


def heartbeat_status() -> dict[str, Any]:
    """Exposed via /api/health so operators can confirm the loop is alive."""
    last = _LAST_HEARTBEAT.get("at")
    if last is None:
        return {"running": False, "last_run_at": None, "seconds_since": None}
    delta = (datetime.now(timezone.utc) - last).total_seconds()
    return {
        "running": True,
        "last_run_at": last.isoformat(),
        "seconds_since": round(delta, 1),
        # Healthy if last run was within 3 poll intervals
        "healthy": delta < POLL_INTERVAL_SEC * 3,
    }


def poll_loop(shutdown_check=None) -> None:
    """Long-running loop. Every POLL_INTERVAL_SEC, pulls up to BATCH_SIZE
    RETRY_PENDING orders due for retry and processes them serially.

    Serial is fine for the scale we care about (~100 subscribers). For
    very high volume the inner _retry_one_order could be parallelized
    via ThreadPoolExecutor — same pattern as copy_engine — but the
    extra complexity isn't justified yet.
    """
    log.info("retry_scheduler: starting (interval=%ss, batch=%d)",
             POLL_INTERVAL_SEC, BATCH_SIZE)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("retry_scheduler: shutdown requested, exiting")
            return

        _LAST_HEARTBEAT["at"] = datetime.now(timezone.utc)
        try:
            with SessionLocal() as db:
                due_ids = list(db.execute(
                    select(Order.id).where(
                        Order.status == OrderStatus.RETRY_PENDING,
                        Order.retry_attempted.is_(False),
                        Order.retry_at <= datetime.now(timezone.utc),
                    ).order_by(Order.retry_at.asc()).limit(BATCH_SIZE)
                ).scalars())

            for order_id in due_ids:
                try:
                    outcome = _retry_one_order(order_id)
                    log.info("retry_scheduler: order=%s outcome=%s", order_id, outcome)
                except Exception:  # noqa: BLE001
                    log.exception("retry_scheduler: error on order=%s", order_id)
        except Exception:  # noqa: BLE001
            log.exception("retry_scheduler: poll iteration failed")

        # Sleep until next poll. Tolerates KeyboardInterrupt cleanly.
        try:
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("retry_scheduler: KeyboardInterrupt, exiting")
            return
