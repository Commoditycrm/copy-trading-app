"""Sends the queued texts (and WhatsApp messages) in sms_notifications and
reads back their delivery.

Runs in the worker (one daemon thread, started only when
REJECTED_TRADE_SMS_ENABLED or REJECTED_TRADE_WHATSAPP_ENABLED is on). Rows are claimed with SKIP LOCKED, so a
row is never sent twice even if two loops overlapped. Nothing here touches
orders: a Twilio outage only delays texts, it can't hold up trading.

Retries: network errors, 429 and 5xx back off (30s, 2m, 10m, 30m) up to
MAX_ATTEMPTS; a 4xx is Twilio refusing the message and is final. Opt-in is
checked again at send time, so turning texts off stops a queued one.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.models.sms_notification import SmsNotification
from app.models.user import User
from app.services import sms

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
BATCH_SIZE = 20
MAX_ATTEMPTS = 5
BACKOFF = [timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(minutes=30)]
# Twilio statuses after which nothing changes
FINAL_TWILIO = {"delivered", "undelivered", "failed", "canceled", "read"}
STATUS_CHECK_EVERY = timedelta(seconds=60)
STATUS_CHECK_FOR = timedelta(hours=48)


def opt_in_problem(user: User | None, pref_attr: str = "sms_on_trade_rejected") -> str | None:
    """Why this user mustn't be texted, or None. The same three switches the
    existing notification texts use; no other state is assumed."""
    if user is None:
        return "no_user"
    if not user.phone:
        return "no_phone"
    if not user.sms_notifications_enabled:
        return "sms_disabled"
    if not getattr(user, pref_attr, False):
        return "category_disabled"
    return None


def whatsapp_opt_in_problem(user: User | None) -> str | None:
    """WhatsApp needs its own recorded opt-in; the SMS switches don't count."""
    if user is None:
        return "no_user"
    if not user.phone:
        return "no_phone"
    if not (user.whatsapp_notifications_enabled and user.whatsapp_opt_in_at):
        return "whatsapp_not_opted_in"
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def send_due(now: datetime | None = None, send=None, send_whatsapp=None) -> int:  # noqa: ANN001
    """Send up to BATCH_SIZE due rows. Returns how many were attempted."""
    from app.services import rejected_trade_sms  # noqa: PLC0415

    now = now or _now()
    send = send or sms.send_sms_detailed
    send_whatsapp = send_whatsapp or sms.send_whatsapp_detailed
    attempted = 0
    with SessionLocal() as db:
        rows = list(db.execute(
            select(SmsNotification)
            .where(SmsNotification.status == "pending",
                   or_(SmsNotification.next_attempt_at.is_(None), SmsNotification.next_attempt_at <= now))
            .order_by(SmsNotification.created_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        ).scalars())
        for row in rows:
            attempted += 1
            user = db.get(User, row.user_id)
            whatsapp = row.category == rejected_trade_sms.WA_CATEGORY
            problem = whatsapp_opt_in_problem(user) if whatsapp else opt_in_problem(user)
            row.updated_at = now
            if problem is None and row.category in (rejected_trade_sms.CATEGORY, rejected_trade_sms.WA_CATEGORY):
                problem = rejected_trade_sms.still_worth_sending(db, row, now)
            if problem:
                row.status, row.skip_reason = "skipped", problem
                continue
            row.attempts += 1
            row.phone_last4 = (user.phone or "")[-4:] or None
            try:
                res = send_whatsapp(user.phone, json.loads(row.body)) if whatsapp else send(user.phone, row.body)
            except Exception as exc:  # noqa: BLE001 — send_sms_detailed never raises; belt and braces
                res = sms.SmsResult(ok=False, error=f"{type(exc).__name__}", transient=True)
            if res.ok:
                row.status, row.sent_at = "sent", now
                row.twilio_sid, row.twilio_status = res.sid, res.status
                row.num_segments = res.num_segments
                row.last_error, row.next_attempt_at = None, None
            elif res.transient and row.attempts < MAX_ATTEMPTS:
                row.last_error = res.error
                row.next_attempt_at = now + BACKOFF[min(row.attempts - 1, len(BACKOFF) - 1)]
            else:
                row.status = "failed"
                row.last_error = res.error or "not sent"
                row.twilio_error_code = res.error_code
                row.next_attempt_at = None
        db.commit()
    return attempted


def _price(v) -> Decimal | None:  # noqa: ANN001
    try:
        return abs(Decimal(str(v))) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def check_statuses(now: datetime | None = None, fetch=None) -> int:  # noqa: ANN001
    """Read Twilio's status for sent texts that haven't settled yet."""
    now = now or _now()
    fetch = fetch or sms.fetch_message
    checked = 0
    with SessionLocal() as db:
        rows = list(db.execute(
            select(SmsNotification)
            .where(SmsNotification.status == "sent",
                   SmsNotification.twilio_sid.is_not(None),
                   SmsNotification.sent_at >= now - STATUS_CHECK_FOR,
                   or_(SmsNotification.status_checked_at.is_(None),
                       SmsNotification.status_checked_at <= now - STATUS_CHECK_EVERY))
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        ).scalars())
        for row in rows:
            checked += 1
            row.status_checked_at = row.updated_at = now
            msg = fetch(row.twilio_sid)
            if not msg:
                continue
            tw = (msg.get("status") or "").lower() or row.twilio_status
            row.twilio_status = tw
            if msg.get("error_code") is not None:
                row.twilio_error_code = sms._int(msg.get("error_code"))  # noqa: SLF001
            row.num_segments = sms._int(msg.get("num_segments")) or row.num_segments  # noqa: SLF001
            if _price(msg.get("price")) is not None:
                row.price, row.price_unit = _price(msg.get("price")), msg.get("price_unit")
            if tw in ("delivered", "read"):
                row.status = "delivered"
            elif tw in ("undelivered", "failed", "canceled"):
                row.status = "undelivered" if tw == "undelivered" else "failed"
                row.last_error = msg.get("error_message") or row.last_error
        db.commit()
    return checked


def poll_loop(shutdown_check=None) -> None:  # noqa: ANN001
    log.info("sms_outbox: starting (interval=%ss)", POLL_INTERVAL_SEC)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("sms_outbox: shutdown requested, exiting")
            return
        for step in (send_due, check_statuses):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("sms_outbox: %s failed", step.__name__)
        time.sleep(POLL_INTERVAL_SEC)
