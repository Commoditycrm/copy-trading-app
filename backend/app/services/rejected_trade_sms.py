"""Rejected-trade texts, decided in one place (REJECTED_TRADE_SMS_ENABLED).

Every order that reaches REJECTED — at placement, from a broker stream or
poll, after retries, from a background close — passes through the hook in
services/rejection_alerts.py, which hands it here after the commit. This
module decides what the rejection actually was, words the text and sends it
once, in the background.

What gets texted:
  broker_rejected  the broker refused the order (at placement or after accepting it)
  failed           our side couldn't place it at all: retries ran out, the
                   broker login is broken, a restart couldn't re-place it
  needs_review     the broker never confirmed it — it MAY have been placed.
                   Worded as such, never as "rejected"
Recorded but not texted:
  blocked          a Kopyya safeguard stopped it on purpose (would open a
                   short, copy turned off, loss limit, trader paused)
  internal         housekeeping (parent or broker account gone)

One text per order: an sms_notifications row keyed on the order id is
inserted first and only the insert that wins sends, so the placement path,
the stream, a poll and the fill sync seeing the same rejection send once.
Retry loops write a NEW order per attempt, so the same contract isn't texted
again within REPEAT_WINDOW.

All of this runs in a background thread after the trade's own commit and
swallows its errors: a failed or slow text never touches order processing.
Twilio's delivery result (delivered / undelivered + error code) is read back
by delivery_loop and shown on Admin → Rejected trades.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderStatus, OrderType
from app.models.sms_notification import SmsNotification
from app.models.user import User
from app.services.order_retry import OUTCOME_UNKNOWN_REASON

log = logging.getLogger(__name__)

CATEGORY = "trade_rejected"
REPEAT_WINDOW = timedelta(minutes=15)   # same contract already texted → don't again
TEXTED = ("broker_rejected", "failed", "needs_review")
DELIVERY_FINAL = ("delivered", "undelivered", "failed")
CHECK_EVERY = timedelta(minutes=2)
CHECK_FOR = timedelta(hours=24)            # Twilio settles well within this
_ET = ZoneInfo("America/New_York")
_ACCEPTED = (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)

# reject_reason texts written by the code paths that aren't a broker refusal
_NEEDS_REVIEW_PREFIXES = (
    "The broker never confirmed this order",            # retry_scheduler.NEEDS_REVIEW_REASON
    "The broker didn't confirm this order",             # copy_engine, first-attempt timeout
    OUTCOME_UNKNOWN_REASON,
)
_BLOCKED_PREFIXES = ("retry_skipped:", "Skipped:")
_INTERNAL_REASONS = ("parent_order_missing", "broker_account_missing")
_FAILED_PREFIXES = ("Broker credentials error:", "recovery_failed:")


def enabled() -> bool:
    return bool(get_settings().rejected_trade_sms_enabled)


def classify(order: Order, previous: OrderStatus | None) -> str:
    """What a REJECTED order really is, from where it came from and the
    reason the writing path recorded."""
    reason = (order.reject_reason or "").strip()
    if reason.startswith(_NEEDS_REVIEW_PREFIXES):
        return "needs_review"
    if reason in _INTERNAL_REASONS:
        return "internal"
    if reason.startswith(_BLOCKED_PREFIXES):
        return "blocked"
    if reason.startswith(_FAILED_PREFIXES):
        return "failed"
    if previous == OrderStatus.RETRY_PENDING:
        return "failed"                                  # retries ran out
    return "broker_rejected"                            # placement refusal, or accepted then rejected


# ── wording ───────────────────────────────────────────────────────────────


def _num(v) -> str:  # noqa: ANN001
    return f"{Decimal(str(v)).normalize():f}"


def contract_label(order: Order) -> str:
    """``TSLA`` or ``TSLA 450C 10/23``."""
    sym = (order.symbol or "?").upper()
    if order.instrument_type != InstrumentType.OPTION:
        return sym
    right = (getattr(order.option_right, "value", "") or "")[:1].upper()
    strike = _num(order.option_strike) if order.option_strike is not None else "?"
    exp = f" {order.option_expiry.month}/{order.option_expiry.day}" if order.option_expiry else ""
    return f"{sym} {strike}{right}{exp}"


def _order_type(order: Order) -> str:
    t = order.order_type
    if t == OrderType.LIMIT and order.limit_price is not None:
        return f"LIMIT ${_num(order.limit_price)}"
    if t == OrderType.STOP and order.stop_price is not None:
        return f"STOP ${_num(order.stop_price)}"
    if t == OrderType.STOP_LIMIT:
        return f"STOP ${_num(order.stop_price or 0)} LIMIT ${_num(order.limit_price or 0)}"
    return (t.value if t else "").upper().replace("_", " ")


def broker_label(acct: BrokerAccount | None) -> str:
    """Brand and paper/live only — never the account number or label."""
    if acct is None:
        return "Unknown"
    name = {BrokerName.ALPACA: "Alpaca", BrokerName.WEBULL: "Webull"}.get(acct.broker)
    if name is None:
        val = getattr(acct.broker, "value", str(acct.broker))
        name = "IBKR" if val == "ibkr" else (acct.brokerage_name or val.title())
    return f"{name} Paper" if acct.is_paper else name


_HTTP_ENVELOPE = re.compile(r"HTTP Status:\s*\d+,\s*Code:\s*([A-Z0-9_]+),\s*Msg:\s*(.*?)(?:,\s*RequestID:.*)?$")
_REQUEST_ID = re.compile(r",?\s*Request ?ID:\s*[\w-]+", re.I)
_ACCOUNTISH = re.compile(r"\b[A-Z]{0,4}\d{6,}\b")
_PREFIXES = re.compile(r"^(broker_error|auto_liquidate_error|position_\w+_error|trader_sl_monitor_error|"
                       r"bracket_exit_error|recovery_failed)\s*:\s*", re.I)


def clean_reason(reason: str | None, outcome: str) -> str:
    """The broker's words, minus request ids, HTTP envelopes and anything
    shaped like an account number."""
    if outcome == "needs_review":
        return "Broker didn't confirm it; it may have been placed. Check your broker before re-entering."
    text = (reason or "").strip()
    if text.startswith("Broker credentials error"):
        return "Broker login problem; reconnect your broker in Kopyya."
    while True:
        stripped = _PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped
    m = _HTTP_ENVELOPE.search(text)
    if m:
        text = f"{m.group(2).strip()} ({m.group(1)})"
    text = _REQUEST_ID.sub("", text)
    text = _ACCOUNTISH.sub("****", text)
    text = re.sub(r"\s+", " ", text).strip(" .,")
    if not text:
        return "No reason given by the broker"
    return text[:110] + ("..." if len(text) > 110 else "")


def _whose(db: Session, order: Order) -> str:
    if order.parent_order_id is not None:
        parent = db.get(Order, order.parent_order_id)
        trader = db.get(User, parent.user_id) if parent is not None else None
        name = gsm_safe(trader.display_name or "") if trader is not None else ""
        return f"Your copy of {(name or 'your trader')[:30].rstrip()}'s"
    return "Your"


# One sentence in the shape of the sample filed on the A2P campaign:
# "Your BUY TSLA order was rejected: <reason>. View: https://kopyya.com/trades"
_VERB = {
    "broker_rejected": "was rejected: {reason}.",
    "failed": "could not be placed: {reason}.",
    "needs_review": "was not confirmed by the broker and may have been placed. Check your broker before re-entering.",
    "blocked": "was not placed: {reason}.",
    "internal": "was not placed: {reason}.",
}


def message_for(db: Session, order: Order, outcome: str, at: datetime) -> str:
    acct = db.get(BrokerAccount, order.broker_account_id) if order.broker_account_id else None
    side = (order.side.value if order.side else "").upper()
    qty = _num(order.quantity or 0)
    when = at.astimezone(_ET)
    stamp = f"{when:%b} {when.day} {when:%I}".replace(" 0", " ").rstrip() + f":{when:%M %p} ET"
    ref = str(order.id).replace("-", "")[:8].upper()
    base = get_settings().frontend_base_url.rstrip("/")
    lead = gsm_safe(f"Kopyya: {_whose(db, order)} {side} {qty} {contract_label(order)} order "
                    f"({_order_type(order)}, {broker_label(acct)})")
    tail = f" {stamp}, Ref {ref}. View: {base}/trades"
    verb = _VERB[outcome]
    if "{reason}" not in verb:
        return f"{lead} {verb}{tail}"
    # the reason gets what's left of two segments once the opt-out is added
    from app.services.sms import compose  # noqa: PLC0415

    room = max(30, MAX_CHARS - len(compose(f"{lead} {verb.format(reason='')}{tail}")))
    reason = gsm_safe(clean_reason(order.reject_reason, outcome)).rstrip(".")
    if len(reason) > room:
        reason = reason[:room - 3].rstrip() + "..."
    return f"{lead} {verb.format(reason=reason)}".replace("....", "...") + tail


# Two concatenated GSM-7 segments. One character outside GSM-7 (an emoji in a
# display name) would switch the whole text to UCS-2 at 67 per segment.
MAX_CHARS = 306
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def gsm_safe(text: str) -> str:
    """Keep only GSM-7 characters (after the usual dash/quote folding)."""
    from app.services.sms import _GSM_FOLD  # noqa: PLC0415

    folded = text.translate(_GSM_FOLD)
    return re.sub(r" {2,}", " ", "".join(ch for ch in folded if ch in _GSM7)).strip()


def subject_key(order: Order) -> str:
    right = getattr(order.option_right, "value", "") or ""
    side = order.side.value if order.side else ""
    return "|".join(str(x) for x in (order.user_id, order.symbol, order.option_expiry or "",
                                     order.option_strike if order.option_strike is not None else "",
                                     right, side))[:160]


# ── sending (after the trade's commit) ────────────────────────────────────


def _insert_once(db: Session, values: dict) -> bool:
    """Insert the order's row; False when another path already did."""
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415
    else:
        from sqlalchemy.dialects.sqlite import insert  # noqa: PLC0415
    stmt = insert(SmsNotification).values(**values).on_conflict_do_nothing(index_elements=["dedup_key"])
    return db.execute(stmt.returning(SmsNotification.id)).first() is not None


def _why_not(db: Session, user: User | None, subject: str, now: datetime) -> str | None:
    """Why this text mustn't go, or None. The user's existing SMS settings
    decide; a contract texted moments ago (a retry loop) isn't texted again."""
    if user is None or not user.phone:
        return "no_phone"
    if not user.sms_notifications_enabled:
        return "sms_disabled"
    if not user.sms_on_trade_rejected:
        return "category_disabled"
    recent = db.scalar(select(SmsNotification.id).where(
        SmsNotification.subject_key == subject, SmsNotification.sent_at >= now - REPEAT_WINDOW).limit(1))
    return "repeat_within_15m" if recent is not None else None


def handle(db: Session, order: Order, previous: OrderStatus | None, now: datetime | None = None,
           send=None) -> str:  # noqa: ANN001
    """Record this rejection once and text it if it should be. Returns the
    row's final status ("sent" / "failed" / "skipped"), or "duplicate"."""
    from app.services.sms import send_sms_sid  # noqa: PLC0415

    send = send or send_sms_sid
    now = now or datetime.now(timezone.utc)
    outcome = classify(order, previous)
    user = db.get(User, order.user_id)
    row_id = uuid.uuid4()
    subject = subject_key(order)
    body = message_for(db, order, outcome, order.closed_at or order.updated_at or now)
    why = None if outcome in TEXTED else f"not_texted_{outcome}"
    why = why or _why_not(db, user, subject, now)
    if not _insert_once(db, {
        "id": row_id, "user_id": order.user_id, "order_id": order.id, "category": CATEGORY,
        "outcome": outcome, "dedup_key": f"{CATEGORY}:{order.id}", "subject_key": subject, "body": body,
        "status": "skipped" if why else "pending", "skip_reason": why, "attempts": 0,
        "created_at": now, "updated_at": now,
    }):
        return "duplicate"
    db.commit()                                   # claim the order before the slow send
    if why:
        return "skipped"
    sid = send(user.phone, body)                  # Twilio message SID, None if refused
    row = db.get(SmsNotification, row_id)
    row.status, row.attempts, row.updated_at = ("failed" if sid is None else "sent"), 1, now
    row.sent_at = None if sid is None else now
    row.twilio_sid = sid or None
    row.phone_last4 = user.phone[-4:]
    db.commit()
    return row.status


def check_delivery(db: Session, now: datetime | None = None, fetch=None) -> int:  # noqa: ANN001
    """Read Twilio's result for texts sent in the last CHECK_FOR; a row becomes
    delivered / undelivered / failed once Twilio knows. Returns rows checked."""
    from app.services.sms import message_status  # noqa: PLC0415

    fetch = fetch or message_status
    now = now or datetime.now(timezone.utc)
    rows = db.execute(select(SmsNotification).where(
        SmsNotification.category == CATEGORY, SmsNotification.status == "sent",
        SmsNotification.twilio_sid.is_not(None), SmsNotification.sent_at >= now - CHECK_FOR,
        or_(SmsNotification.status_checked_at.is_(None), SmsNotification.status_checked_at <= now - CHECK_EVERY),
    ).limit(50)).scalars().all()
    for row in rows:
        msg = fetch(row.twilio_sid) or {}
        row.status_checked_at = row.updated_at = now
        row.twilio_status = msg.get("status") or row.twilio_status
        try:
            row.twilio_error_code = int(msg["error_code"]) if msg.get("error_code") else row.twilio_error_code
        except (TypeError, ValueError):
            pass
        if msg.get("error_message"):
            row.last_error = str(msg["error_message"])[:300]
        if row.twilio_status in DELIVERY_FINAL:
            row.status = row.twilio_status
    db.commit()
    return len(rows)


def delivery_loop(shutdown_check) -> None:  # noqa: ANN001
    from app.database import SessionLocal  # noqa: PLC0415

    while not shutdown_check():
        try:
            with SessionLocal() as db:
                check_delivery(db)
        except Exception:  # noqa: BLE001
            log.exception("rejected_trade_sms: delivery check failed")
        for _ in range(int(CHECK_EVERY.total_seconds())):
            if shutdown_check():
                return
            time.sleep(1)


def _run(items: list[tuple[uuid.UUID, OrderStatus | None]]) -> None:
    from app.database import SessionLocal  # noqa: PLC0415

    for oid, previous in items:
        try:
            with SessionLocal() as db:
                order = db.get(Order, oid)
                if order is not None and order.status == OrderStatus.REJECTED and order.user_id is not None:
                    handle(db, order, previous)
        except Exception:  # noqa: BLE001
            log.exception("rejected_trade_sms: could not handle order %s", oid)


def queue_after_commit(items: list[tuple[uuid.UUID, OrderStatus | None]]) -> None:
    """Called by the rejection hook once the rejection is committed. Hands the
    work to a background thread; never raises, never blocks the caller."""
    try:
        threading.Thread(target=_run, args=(items,), daemon=True, name="rejected-trade-sms").start()
    except Exception:  # noqa: BLE001
        log.exception("rejected_trade_sms: could not start the sender")


__all__ = ["check_delivery", "classify", "clean_reason", "contract_label", "delivery_loop", "enabled",
           "handle", "message_for", "queue_after_commit"]
