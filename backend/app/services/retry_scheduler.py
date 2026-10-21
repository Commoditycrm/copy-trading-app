"""Background scheduler that retries failed subscriber mirror orders.

Picks up Order rows where status=RETRY_PENDING AND retry_at <= now().
Re-runs the gate checks (subscriber's copy_enabled, daily_loss_limit,
trader's master switch — same checks that ran the first time). If they
all pass, attempts the broker call again via place_order_with_recovery.

On success → status=SUBMITTED, broker_order_id filled in, audit
``copy.retry_succeeded``, SSE event so subscriber UI updates.

On failure and retries remaining → status stays RETRY_PENDING, retry_count
incremented, retry_at pushed forward by the subscriber's retry interval.

On final failure (retry_count == retry_max_attempts) → status=REJECTED,
audit ``copy.retry_failed``, persistent Notification for the subscriber.

Unconfirmed sends
-----------------
A timeout may have placed the order. Before re-placing, the broker is asked
whether it has it; a match is adopted (``copy.retry_adopted``), not re-sent.
Brokers that refuse a repeated client_order_id (Alpaca, Webull) can always be
re-sent safely. Others (SnapTrade) are NEVER re-sent after an unconfirmed
send: the order is adopted only if the contract lookup finds exactly one and
no other order of ours on the account could own it; otherwise the mirror goes
to manual review (``copy.retry_needs_review``).

Retry count
-----------
The subscriber controls how many additional attempts are made via
``retry_max_attempts`` (1–5, default 1). ``retry_count`` on the Order row
tracks how many attempts have been made so far (starts at 0 after the
original failure). On every retry:
  - retry_count += 1
  - if retry_count < retry_max_attempts → reschedule (RETRY_PENDING again)
  - if retry_count >= retry_max_attempts → REJECTED + notify

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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.notification import Notification  # noqa: F401  — ORM registration
from app.models.order import Order, OrderStatus
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User
from app.services import audit, events
from app.services.copy_engine import _order_event, _RETRY_INTERVAL_MINUTES
from app.services.crypto import decrypt_json
from app.services.notifications import create_notification
from app.services.order_retry import (
    OUTCOME_UNKNOWN_REASON,
    AdoptedOrderResult,
    OrderOutcomeUnknown,
    RecoverableOrderError,
    lookup_placed_order,
    place_order_with_recovery,
)
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
    attempts_made: int,
) -> None:
    """Drop a persistent notification on the subscriber telling them
    their mirror retry didn't make it.

    The reason shown is the SAME reject_reason stored on the order (what the
    admin panel reads), so the subscriber's notification can't diverge from
    admin. Callers must set ``child.reject_reason`` before calling."""
    trader_name = _trader_email(db, trader_order.user_id)
    symbol = trader_order.symbol
    side = trader_order.side.value.upper()
    qty = str(child.quantity)
    instrument = "option" if trader_order.instrument_type.value == "option" else "share"
    retry_word = "retry" if attempts_made == 1 else "retries"
    reason = child.reject_reason or "broker was unreachable"

    message = (
        f"Your mirror of {trader_name}'s {side} {qty} {symbol} "
        f"{instrument} order failed after {attempts_made} {retry_word}. "
        f"Reason: {reason}"
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
            "reason": reason,
            "trader_id": str(trader_order.user_id),
            "trader_name": trader_name,
            "attempts_made": attempts_made,
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


NEEDS_REVIEW_REASON = (
    "The broker never confirmed this order and it couldn't be verified, so it "
    "wasn't re-sent. Check your broker account before placing it yourself."
)


def _reschedule(child: Order, sub_settings: SubscriberSettings) -> None:
    interval = (
        sub_settings.retry_interval_close if child.is_closing
        else sub_settings.retry_interval_open
    )
    minutes = _RETRY_INTERVAL_MINUTES.get(interval, 1)
    child.status = OrderStatus.RETRY_PENDING
    child.retry_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _possible_owners(db: Session, child: Order, placed_after: datetime) -> int:
    """Our other orders on this account that a contract match can't be told
    apart from: same instrument, side and quantity, active around the send.
    Counted whatever their broker_order_id — null if their send also went
    unconfirmed, or no longer the id the broker lists if it changed it."""
    lo = placed_after - timedelta(hours=1)
    return db.scalar(
        select(func.count()).select_from(Order).where(
            Order.broker_account_id == child.broker_account_id,
            Order.id != child.id,
            Order.instrument_type == child.instrument_type,
            Order.symbol == child.symbol,
            Order.side == child.side,
            Order.quantity == child.quantity,
            Order.option_expiry.is_not_distinct_from(child.option_expiry),
            Order.option_strike.is_not_distinct_from(child.option_strike),
            Order.option_right.is_not_distinct_from(child.option_right),
            or_(Order.created_at >= lo, Order.updated_at >= lo),
        )
    ) or 0


def _needs_review(
    db: Session, child: Order, trader_order: Order, error: str, *, counted: bool = False,
) -> str:
    """Stop retrying a mirror whose placement can't be proven either way."""
    if not counted:
        child.retry_count += 1
    child.status = OrderStatus.REJECTED
    child.reject_reason = NEEDS_REVIEW_REASON
    child.closed_at = datetime.now(timezone.utc)
    audit.record(
        db, actor_user_id=child.user_id, action="copy.retry_needs_review",
        entity_type="order", entity_id=child.id,
        metadata={"parent_order_id": str(trader_order.id), "error": error[:300],
                  "attempts_made": child.retry_count},
    )
    _notify_retry_failed(db, child, trader_order, child.retry_count)
    db.commit()
    events.publish(child.user_id, _order_event("order.copy_failed", child))
    return "needs_review"


def _hold_unverified(
    db: Session, child: Order, trader_order: Order,
    sub_settings: SubscriberSettings, max_attempts: int, lookup_state: str,
) -> str:
    """The lookup couldn't run: check again later, or give up to review."""
    child.retry_count += 1
    if child.retry_count >= max_attempts:
        return _needs_review(db, child, trader_order, f"order lookup {lookup_state}", counted=True)
    _reschedule(child, sub_settings)
    child.reject_reason = OUTCOME_UNKNOWN_REASON
    audit.record(
        db, actor_user_id=child.user_id, action="copy.retry_rescheduled",
        entity_type="order", entity_id=child.id,
        metadata={"attempt": child.retry_count, "max_attempts": max_attempts,
                  "retry_at": child.retry_at.isoformat(), "lookup": lookup_state,
                  "outcome_unknown": True},
    )
    db.commit()
    events.publish(child.user_id, _order_event("order.copy_retry_scheduled", child))
    return "rescheduled"


# ── per-order retry ─────────────────────────────────────────────────────────

def _retry_one_order(order_id: uuid.UUID) -> str:
    """Pick up one RETRY_PENDING order, run gates + broker call. Returns
    a short outcome string for logging: "succeeded" / "adopted" /
    "rescheduled" / "needs_review" / "gate_failed:<reason>" /
    "broker_failed" / "vanished"."""
    with SessionLocal() as db:
        child = db.get(Order, order_id)
        if child is None or child.status != OrderStatus.RETRY_PENDING:
            return "vanished"

        trader_order = db.get(Order, child.parent_order_id) if child.parent_order_id else None
        if trader_order is None:
            child.status = OrderStatus.REJECTED
            child.retry_count += 1
            child.reject_reason = "parent_order_missing"
            child.closed_at = datetime.now(timezone.utc)
            db.commit()
            return "vanished"

        # Load subscriber settings to get retry_max_attempts
        sub_settings = db.get(SubscriberSettings, child.user_id)
        max_attempts = sub_settings.retry_max_attempts if sub_settings else 1

        # Re-run the gates on FRESH state
        gate_skip = _passes_gates(db, child, trader_order)
        if gate_skip:
            child.status = OrderStatus.REJECTED
            child.retry_count += 1
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
            child.retry_count += 1
            child.reject_reason = "broker_account_missing"
            child.closed_at = datetime.now(timezone.utc)
            db.commit()
            return "vanished"

        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
        except Exception as exc:  # noqa: BLE001
            child.retry_count += 1
            new_count = child.retry_count
            child.status = OrderStatus.REJECTED
            child.reject_reason = f"Broker credentials error: {exc}"[:480]
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_failed",
                entity_type="order", entity_id=child.id,
                metadata={"reason": "credentials_error", "error": str(exc)[:300]},
            )
            _notify_retry_failed(db, child, trader_order, new_count)
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

        # An earlier send may already have landed. Deduping brokers are always
        # checked (one by-id read); contract-matched ones only after a send we
        # couldn't confirm, since a 429/refusal placed nothing.
        unknown_before = (child.reject_reason or "").startswith(OUTCOME_UNKNOWN_REASON)
        dedupes = getattr(adapter, "dedupes_client_order_id", False)
        placed_after = child.created_at or (datetime.now(timezone.utc) - timedelta(hours=1))
        resp = None
        if dedupes:
            _, resp = lookup_placed_order(adapter, request, placed_after=placed_after)
        elif unknown_before:
            # A re-send isn't deduped here, so reconcile or escalate — never
            # send. Adopt only an order that can be nobody else's.
            owners = _possible_owners(db, child, placed_after)
            if owners:
                return _needs_review(
                    db, child, trader_order,
                    f"{owners} other order(s) on this account share the contract/side/qty",
                )
            state, resp = lookup_placed_order(adapter, request, placed_after=placed_after)
            if resp is None and state in ("absent", "ambiguous"):
                return _needs_review(db, child, trader_order, f"order lookup {state}")
            if resp is None:
                return _hold_unverified(db, child, trader_order, sub_settings, max_attempts, state)

        # Broker call — succeed, fail, or surface a clean reason.
        broker_exc: Exception | None = None
        broker_friendly: str | None = None
        if resp is None:
            try:
                resp = place_order_with_recovery(adapter, request)
            except RecoverableOrderError as rec:
                broker_exc = rec
                broker_friendly = rec.friendly_message
            except Exception as exc:  # noqa: BLE001
                broker_exc = exc
                broker_friendly = None

        if broker_exc is None:
            # Success on retry
            adopted = isinstance(resp, AdoptedOrderResult)
            child.status = resp.status
            child.broker_order_id = resp.broker_order_id
            child.submitted_at = resp.submitted_at
            child.filled_quantity = resp.filled_quantity
            child.filled_avg_price = resp.filled_avg_price
            child.retry_count += 1
            child.reject_reason = None
            audit.record(
                db, actor_user_id=child.user_id,
                action="copy.retry_adopted" if adopted else "copy.retry_succeeded",
                entity_type="order", entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "broker_order_id": resp.broker_order_id,
                    "attempt": child.retry_count,
                },
            )
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_submitted", child))
            return "adopted" if adopted else "succeeded"

        # Broker call failed — decide whether to reschedule or give up.
        child.retry_count += 1
        new_count = child.retry_count

        # This send went unconfirmed: the next run only looks it up.
        if isinstance(broker_exc, OrderOutcomeUnknown) and new_count < max_attempts:
            _reschedule(child, sub_settings)
            child.reject_reason = OUTCOME_UNKNOWN_REASON
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_rescheduled",
                entity_type="order", entity_id=child.id,
                metadata={"attempt": new_count, "max_attempts": max_attempts,
                          "retry_at": child.retry_at.isoformat(),
                          "error": str(broker_exc)[:300], "outcome_unknown": True},
            )
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_retry_scheduled", child))
            return "rescheduled"
        if isinstance(broker_exc, OrderOutcomeUnknown):
            return _needs_review(db, child, trader_order, str(broker_exc.original), counted=True)

        if new_count < max_attempts:
            # More retries allowed — reschedule.
            _reschedule(child, sub_settings)
            reason_str = broker_friendly or str(broker_exc)
            child.reject_reason = f"transient broker error, will retry ({new_count}/{max_attempts})"
            audit.record(
                db, actor_user_id=child.user_id, action="copy.retry_rescheduled",
                entity_type="order", entity_id=child.id,
                metadata={
                    "attempt": new_count,
                    "max_attempts": max_attempts,
                    "retry_at": child.retry_at.isoformat(),
                    "error": reason_str[:300],
                },
            )
            db.commit()
            events.publish(child.user_id, _order_event("order.copy_retry_scheduled", child))
            return "rescheduled"

        # Hit the max — final failure.
        child.status = OrderStatus.REJECTED
        child.reject_reason = (broker_friendly or str(broker_exc))[:480]
        child.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=child.user_id, action="copy.retry_failed",
            entity_type="order", entity_id=child.id,
            metadata={
                "friendly": broker_friendly,
                "raw": str(broker_exc)[:300],
                "attempts_made": new_count,
                "classification": (
                    "user_fixable_on_retry"
                    if isinstance(broker_exc, RecoverableOrderError)
                    else "still_transient_or_unknown"
                ),
            },
        )
        _notify_retry_failed(db, child, trader_order, new_count)
        db.commit()
        events.publish(child.user_id, _order_event("order.copy_failed", child))
        return "broker_failed"


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
    RETRY_PENDING orders due for retry and processes them serially."""
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

        try:
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("retry_scheduler: KeyboardInterrupt, exiting")
            return
