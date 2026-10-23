"""SMS usage and spending, for Admin → System → SMS costs.

Read from Twilio's own billing (Usage Records, SMS category): this month by
day, this month and last month. Account-wide: every environment that uses
these Twilio credentials, not just this server. Twilio credentials never
leave the backend; results are cached so reloading the page doesn't hit
Twilio.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_API = "https://api.twilio.com/2010-04-01"
CACHE_TTL_S = 30 * 60
MIN_REFRESH_S = 5 * 60
_cache: dict[str, Any] = {}
_lock = threading.Lock()


def _get(path: str, params: dict | None = None) -> dict:
    s = get_settings()
    r = httpx.get(f"{_API}/Accounts/{s.twilio_account_sid}{path}", params=params,
                  auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=20)
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


def build(now: datetime, get: Callable = _get) -> dict[str, Any]:
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return {"status": "unavailable", "reason": "Twilio isn't configured on this server."}
    today = now.date()
    usage = twilio_usage(today, get)
    this, last = usage["this_month"], usage["last_month"]
    return {
        "status": "ok",
        "currency": usage["unit"],
        "scope": "Twilio account (every environment using these credentials)",
        "month": today.strftime("%Y-%m"),
        "messages": int(this.get("count") or 0),
        "segments": int(this.get("usage") or 0),
        "cost_usd": _money(_dec(this.get("price"))),
        "last_month": {"messages": int(last.get("count") or 0), "cost_usd": _money(_dec(last.get("price"))),
                       "month": (last.get("start_date") or "")[:7]},
        "daily": [{"date": d["date"], "messages": d["messages"], "segments": d["segments"],
                   "cost_usd": _money(d["cost"])} for d in usage["days"]],
        "fetched_at": now.isoformat(),
    }


def read(refresh: bool = False, now: datetime | None = None, get: Callable = _get) -> dict[str, Any]:
    """Cached for CACHE_TTL_S; a refresh is honoured at most every MIN_REFRESH_S."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        hit = _cache.get("data")
        at = _cache.get("at", 0.0)
    age = time.monotonic() - at
    if hit is not None and (age < CACHE_TTL_S and not (refresh and age >= MIN_REFRESH_S)):
        return {**hit, "cached_for_s": int(age)}
    try:
        data = build(now, get)
    except Exception as exc:  # noqa: BLE001
        log.warning("sms_costs: Twilio read failed: %s", type(exc).__name__)
        if hit is not None:
            return {**hit, "cached_for_s": int(age), "last_error": f"Twilio read failed ({type(exc).__name__})"}
        return {"status": "unavailable", "reason": f"Couldn't read Twilio usage ({type(exc).__name__})."}
    with _lock:
        _cache["data"], _cache["at"] = data, time.monotonic()
    return {**data, "cached_for_s": 0}
