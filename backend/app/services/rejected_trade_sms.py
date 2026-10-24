"""Rejected-trade texts, decided in one place (REJECTED_TRADE_SMS_ENABLED).

Every order that reaches REJECTED — at placement, from a broker stream or
poll, after retries, from a background close — passes through the hook in
services/rejection_alerts.py, which hands it here after the commit. This
module decides what the rejection actually was, words the text, and queues it
in sms_notifications (sent by services/sms_outbox.py).

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

One text per order: the row's dedup_key is the order id and inserts ignore a
conflict, so the placement path, the stream, a poll and the fill sync seeing
the same rejection make one row. Retry loops write a NEW order per attempt,
so a text also waits SEND_DELAY and is dropped if the same contract was
texted recently or a later attempt went through.

All of this runs after the trade's own commit and swallows its errors: a
failed or slow text never touches order processing.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderStatus, OrderType
from app.models.sms_notification import SmsNotification
from app.models.user import User
from app.services.order_retry import OUTCOME_UNKNOWN_REASON

log = logging.getLogger(__name__)

CATEGORY = "trade_rejected"
SEND_DELAY = timedelta(seconds=60)      # lets a fallback/retry attempt land first
REPEAT_WINDOW = timedelta(minutes=15)   # same contract already texted → don't again
TEXTED = ("broker_rejected", "failed", "needs_review")
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
    label = get_settings().sms_environment_label.strip()
    brand = f"Kopyya {label}".strip()
    acct = db.get(BrokerAccount, order.broker_account_id) if order.broker_account_id else None
    side = (order.side.value if order.side else "").upper()
    qty = _num(order.quantity or 0)
    when = at.astimezone(_ET)
    stamp = f"{when:%b} {when.day} {when:%I}".replace(" 0", " ").rstrip() + f":{when:%M %p} ET"
    ref = str(order.id).replace("-", "")[:8].upper()
    base = get_settings().frontend_base_url.rstrip("/")
    lead = gsm_safe(f"{brand}: {_whose(db, order)} {side} {qty} {contract_label(order)} order "
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


# ── queueing (after the trade's commit) ───────────────────────────────────


def _insert_ignore(db: Session, values: dict) -> None:
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415
    else:
        from sqlalchemy.dialects.sqlite import insert  # noqa: PLC0415
    db.execute(insert(SmsNotification).values(**values).on_conflict_do_nothing(index_elements=["dedup_key"]))


def queue(db: Session, order: Order, previous: OrderStatus | None, now: datetime | None = None) -> None:
    """Record this rejection once. Texted outcomes wait SEND_DELAY in
    'pending'; the rest are kept as 'skipped' so the admin view shows why."""
    now = now or datetime.now(timezone.utc)
    outcome = classify(order, previous)
    at = order.closed_at or order.updated_at or now
    texted = outcome in TEXTED
    _insert_ignore(db, {
        "id": uuid.uuid4(), "user_id": order.user_id, "order_id": order.id,
        "category": CATEGORY, "outcome": outcome, "dedup_key": f"{CATEGORY}:{order.id}",
        "subject_key": subject_key(order), "body": message_for(db, order, outcome, at),
        "status": "pending" if texted else "skipped",
        "skip_reason": None if texted else f"not_texted_{outcome}",
        "attempts": 0, "next_attempt_at": now + SEND_DELAY if texted else None,
        "created_at": now, "updated_at": now,
    })


def queue_after_commit(items: list[tuple[uuid.UUID, OrderStatus | None]]) -> None:
    """Called by the rejection hook once the rejection is committed. Own
    session; never raises."""
    from app.database import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            for oid, previous in items:
                order = db.get(Order, oid)
                if order is None or order.status != OrderStatus.REJECTED or order.user_id is None:
                    continue
                queue(db, order, previous)
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("rejected_trade_sms: could not queue %s", [str(i) for i, _ in items])


def still_worth_sending(db: Session, row: SmsNotification, now: datetime) -> str | None:
    """Last look before the text goes: a reason to drop it, or None."""
    order = db.get(Order, row.order_id) if row.order_id else None
    if order is None:
        return "order_gone"
    if order.status != OrderStatus.REJECTED:
        return "no_longer_rejected"           # e.g. adopted by a later lookup
    if row.subject_key:
        recent = db.scalar(select(SmsNotification.id).where(
            SmsNotification.subject_key == row.subject_key,
            SmsNotification.id != row.id,
            SmsNotification.status.in_(("sent", "delivered", "undelivered")),
            SmsNotification.sent_at >= now - REPEAT_WINDOW,
        ).limit(1))
        if recent is not None:
            return "repeat_within_15m"
    # A later attempt at the same thing that the broker took (market → limit
    # fallback, a re-placed take-profit). A resting stop placed afterwards is
    # a different order, not the same one going through.
    stops = (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)
    same_kind = (Order.order_type.in_(stops) if order.order_type in stops
                 else Order.order_type.notin_(stops))
    later = db.scalar(select(Order.id).where(
        Order.user_id == order.user_id, Order.id != order.id,
        Order.symbol == order.symbol, Order.side == order.side,
        Order.quantity == order.quantity, same_kind,
        Order.option_expiry.is_not_distinct_from(order.option_expiry),
        Order.option_strike.is_not_distinct_from(order.option_strike),
        Order.option_right.is_not_distinct_from(order.option_right),
        Order.created_at >= order.created_at,
        Order.status.notin_((OrderStatus.REJECTED, OrderStatus.PENDING, OrderStatus.RETRY_PENDING)),
    ).limit(1))
    if later is not None:
        return "superseded_by_later_attempt"
    return None


__all__ = ["classify", "clean_reason", "contract_label", "enabled", "message_for",
           "queue", "queue_after_commit", "still_worth_sending"]
