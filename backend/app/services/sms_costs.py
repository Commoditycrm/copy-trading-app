"""SMS usage and spending, for Admin → System → SMS costs.

Two sources, kept apart and labelled:
  Twilio account   Twilio's own billing (Usage Records: what was charged, by day
                   and month) and its message log (delivery status, errors,
                   segments). Account-wide: every environment that uses these
                   Twilio credentials, not just this server.
  This server      the texts this environment sent and tracked itself
                   (sms_notifications: rejected-trade texts, with the price
                   Twilio reported per message).
WhatsApp is reported apart from SMS: Twilio bills it under the
channels-whatsapp (Meta template fees) and channels-messaging (Twilio's
per-message fee) categories, and its messages are "whatsapp:"-addressed in the
log. The projection is an estimate — month to date plus the average of the last
seven billed days — and labelled as one. Twilio credentials never leave the
backend; results are cached so reloading the page doesn't hit Twilio.
"""
from __future__ import annotations

import calendar
import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.sms_notification import SmsNotification

log = logging.getLogger(__name__)

_API = "https://api.twilio.com/2010-04-01"
CACHE_TTL_S = 30 * 60
MIN_REFRESH_S = 5 * 60
MAX_MESSAGES = 5000                         # message-log pages read per month
_cache: dict[str, Any] = {}
_lock = threading.Lock()


def _get(path: str, params: dict | None = None) -> dict:
    s = get_settings()
    url = path if path.startswith("http") else f"{_API}/Accounts/{s.twilio_account_sid}{path}"
    r = httpx.get(url, params=params, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=20)
    r.raise_for_status()
    return r.json()


def _dec(v) -> Decimal:  # noqa: ANN001
    try:
        return abs(Decimal(str(v or "0")))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _money(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(Decimal("0.0001")))


def twilio_usage(today: date, get: Callable = _get) -> dict:
    """Billed SMS usage: this month by day, this month and last month totals."""
    month_start = today.replace(day=1)
    daily = get("/Usage/Records/Daily.json", {"Category": "sms", "StartDate": month_start.isoformat(),
                                               "EndDate": today.isoformat(), "PageSize": 100})
    days = sorted(({"date": u["start_date"], "messages": int(u.get("count") or 0),
                    "segments": int(u.get("usage") or 0), "cost": _dec(u.get("price"))}
                   for u in daily.get("usage_records") or []), key=lambda d: d["date"])
    this = (get("/Usage/Records/ThisMonth.json", {"Category": "sms"}).get("usage_records") or [{}])[0]
    last = (get("/Usage/Records/LastMonth.json", {"Category": "sms"}).get("usage_records") or [{}])[0]
    return {"days": days, "this_month": this, "last_month": last,
            "unit": (this.get("price_unit") or "usd").upper()}


def whatsapp_usage(get: Callable = _get) -> dict:
    """Billed WhatsApp usage, this month and last month."""
    out = {}
    for period in ("ThisMonth", "LastMonth"):
        meta = (get(f"/Usage/Records/{period}.json", {"Category": "channels-whatsapp"}).get("usage_records") or [{}])[0]
        fee = (get(f"/Usage/Records/{period}.json", {"Category": "channels-messaging"}).get("usage_records") or [{}])[0]
        out[period] = {"messages": int(float(meta.get("count") or 0)),
                       "cost": _dec(meta.get("price")) + _dec(fee.get("price")),
                       "month": (meta.get("start_date") or fee.get("start_date") or "")[:7]}
    return out


# The message log also holds WhatsApp (and other channel) messages, addressed
# "whatsapp:+1…"; they aren't SMS and Twilio bills them under other categories.
_OTHER_CHANNELS = ("whatsapp:", "messenger:", "rcs:")


def _is_sms(m: dict) -> bool:
    return not any(str(m.get(k) or "").startswith(_OTHER_CHANNELS) for k in ("to", "from"))


def twilio_messages(today: date, get: Callable = _get) -> dict:
    """Outbound SMS this month: delivery status, errors, segments, price."""
    status, errors, wa_status = Counter(), Counter(), Counter()
    segments, cost, n, page, other = 0, Decimal(0), 0, 0, 0
    nxt: str | None = "/Messages.json"
    params: dict | None = {"DateSent>": today.replace(day=1).isoformat(), "PageSize": 1000}
    while nxt and n < MAX_MESSAGES:
        data = get(nxt if nxt.startswith("/Messages") else f"https://api.twilio.com{nxt}", params)
        params = None
        for m in data.get("messages") or []:
            if not str(m.get("direction", "")).startswith("outbound"):
                continue
            if not _is_sms(m):
                other += 1
                if str(m.get("to") or "").startswith("whatsapp:"):
                    wa_status[m.get("status") or "unknown"] += 1
                continue
            n += 1
            status[m.get("status") or "unknown"] += 1
            if m.get("error_code"):
                errors[int(m["error_code"])] += 1
            segments += int(m.get("num_segments") or 0)
            cost += _dec(m.get("price"))
        nxt = data.get("next_page_uri")
        page += 1
    return {"sent": n, "status": dict(status), "errors": dict(errors.most_common(5)),
            "segments": segments, "cost": cost, "truncated": bool(nxt), "other_channels": other,
            "whatsapp_status": dict(wa_status)}


def tracked(db: Session, today: date, category: str = "trade_rejected") -> dict:
    """Texts (or, by category, WhatsApp messages) this server sent and tracked."""
    start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    rows = db.execute(
        select(SmsNotification.status, func.count(), func.coalesce(func.sum(SmsNotification.num_segments), 0),
               func.coalesce(func.sum(SmsNotification.price), 0))
        .where(SmsNotification.category == category, SmsNotification.created_at >= start)
        .group_by(SmsNotification.status)
    ).all()
    by = {s: {"count": c, "segments": int(seg), "cost": _dec(p)} for s, c, seg, p in rows}
    sent = sum(v["count"] for k, v in by.items() if k in ("sent", "delivered", "undelivered", "failed"))
    return {"rejected_trade_texts": sent, "by_status": {k: v["count"] for k, v in by.items()},
            "segments": sum(v["segments"] for v in by.values()),
            "cost": sum((v["cost"] for v in by.values()), Decimal(0))}


def projection(days: list[dict], today: date) -> dict:
    """Month to date + the average of the last 7 billed days × days left. An
    estimate; unavailable with no billed days."""
    mtd = sum((d["cost"] for d in days), Decimal(0))
    past = [d for d in days if d["date"] < today.isoformat()][-7:]
    if not past:
        return {"estimate_usd": None, "basis": "no billed days yet"}
    avg = sum((d["cost"] for d in past), Decimal(0)) / len(past)
    left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    return {"estimate_usd": _money(mtd + avg * left), "daily_average_usd": _money(avg),
            "basis": f"month to date + average of the last {len(past)} billed days × {left} days left"}


def build(db: Session, now: datetime, get: Callable = _get) -> dict[str, Any]:
    s = get_settings()
    today = now.date()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return {"status": "unavailable", "reason": "Twilio isn't configured on this server.",
                "this_server": _jsonable(tracked(db, today)),
                "whatsapp_this_server": _jsonable(tracked(db, today, "trade_rejected_whatsapp"))}
    usage = twilio_usage(today, get)
    msgs = twilio_messages(today, get)
    wa = whatsapp_usage(get)
    wa_st = msgs["whatsapp_status"]
    this, last = usage["this_month"], usage["last_month"]
    errors = msgs["errors"]
    return {
        "status": "ok",
        "currency": usage["unit"],
        "twilio_account": {
            "scope": "Twilio account (every environment using these credentials)",
            "month": today.strftime("%Y-%m"),
            "messages": int(this.get("count") or 0),
            "segments": int(this.get("usage") or 0),
            "cost_usd": _money(_dec(this.get("price"))),
            "last_month": {"messages": int(last.get("count") or 0), "cost_usd": _money(_dec(last.get("price"))),
                           "month": (last.get("start_date") or "")[:7]},
            "daily": [{"date": d["date"], "messages": d["messages"], "segments": d["segments"],
                       "cost_usd": _money(d["cost"])} for d in usage["days"]],
            "delivery": {"sent": msgs["sent"], "delivered": msgs["status"].get("delivered", 0),
                         "undelivered": msgs["status"].get("undelivered", 0), "failed": msgs["status"].get("failed", 0),
                         "in_flight": sum(v for k, v in msgs["status"].items() if k in ("queued", "accepted", "sending", "sent")),
                         "segments": msgs["segments"], "cost_usd": _money(msgs["cost"]),
                         "top_errors": [{"code": k, "count": v} for k, v in errors.items()],
                         "truncated": msgs["truncated"], "excluded_other_channels": msgs["other_channels"]},
            "unregistered_blocked": errors.get(30034, 0),
        },
        "projection": projection(usage["days"], today),
        "this_server": _jsonable(tracked(db, today)),
        "whatsapp": {
            "messages": wa["ThisMonth"]["messages"], "cost_usd": _money(wa["ThisMonth"]["cost"]),
            "last_month": {"messages": wa["LastMonth"]["messages"], "cost_usd": _money(wa["LastMonth"]["cost"]),
                           "month": wa["LastMonth"]["month"]},
            "delivery": {"sent": sum(wa_st.values()), "delivered": wa_st.get("delivered", 0) + wa_st.get("read", 0),
                         "undelivered": wa_st.get("undelivered", 0), "failed": wa_st.get("failed", 0)},
        },
        "whatsapp_this_server": _jsonable(tracked(db, today, "trade_rejected_whatsapp")),
        "fetched_at": now.isoformat(),
    }


def _jsonable(t: dict) -> dict:
    return {**t, "cost": _money(t["cost"])}


def read(db: Session, refresh: bool = False, now: datetime | None = None, get: Callable = _get) -> dict[str, Any]:
    """Cached for CACHE_TTL_S; a refresh is honoured at most every MIN_REFRESH_S."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        hit = _cache.get("data")
        at = _cache.get("at", 0.0)
    age = time.monotonic() - at
    if hit is not None and (age < CACHE_TTL_S and not (refresh and age >= MIN_REFRESH_S)):
        return {**hit, "cached_for_s": int(age)}
    try:
        data = build(db, now, get)
    except Exception as exc:  # noqa: BLE001
        log.warning("sms_costs: Twilio read failed: %s", type(exc).__name__)
        if hit is not None:
            return {**hit, "cached_for_s": int(age), "last_error": f"Twilio read failed ({type(exc).__name__})"}
        return {"status": "unavailable", "reason": f"Couldn't read Twilio usage ({type(exc).__name__})."}
    with _lock:
        _cache["data"], _cache["at"] = data, time.monotonic()
    return {**data, "cached_for_s": 0}
