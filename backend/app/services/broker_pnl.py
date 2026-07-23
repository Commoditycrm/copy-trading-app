"""Realized P&L computed DIRECTLY from the broker's own activity feed, not our
DB order/fill records.

Why this exists
---------------
`pnl.realized_pnl_by_day` FIFOs over our stored orders/fills. That drifts from
the broker whenever the listener misses a close (recorded as canceled), so the
Calendar can under- or mis-count. This module skips our DB entirely: it pulls
the COMPLETE trade history straight from SnapTrade
(`SnapTradeAdapter.get_account_activities`) and FIFOs that — the same data the
broker's own app shows.

Scope: this is REALIZED P&L (closed trades). It matches the broker's app on days
a position is opened and closed; it does NOT include unrealized mark-to-market on
positions held overnight (that needs the account balance-history endpoint, which
is entitlement-gated / 1141 for our keys). See the calendar endpoint for how it's
wired with a DB fallback.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.pnl import _tz_or_market  # shared ET/tz bucketing

log = logging.getLogger(__name__)


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _contract(activity: Any) -> tuple[tuple, Decimal]:
    """(contract_key, unit_multiplier) from a SnapTrade activity. Options carry
    an OCC ``option_symbol.ticker`` (e.g. 'SPXW  260722C07550000'); the last 15
    chars are yymmdd(6)+right(1)+strike(8), the rest is the root. Stocks have no
    option_symbol — key on the plain symbol, multiplier 1."""
    opt = _attr(activity, "option_symbol")
    ticker = _attr(opt, "ticker") if opt else None
    if ticker and len(str(ticker)) >= 15:
        t = str(ticker)
        core = t[-15:]
        return (t[:-15].strip(), core[6], core[7:], core[:6]), Decimal(100)
    sym = _attr(activity, "symbol", default="")
    return (str(sym), "", "", ""), Decimal(1)


def realized_by_day_from_broker(
    adapter: Any,
    start: date,
    end: date,
    tz_name: str | None = None,
) -> dict[date, tuple[Decimal, int]]:
    """{day: (realized_pnl, closing_trade_count)} for [start, end], FIFO over the
    broker's own trade activities. Raises on a broker/API failure so the caller
    can fall back to the DB rather than silently returning an empty calendar."""
    activities = adapter.get_account_activities(start.isoformat(), end.isoformat())
    bucket_tz = _tz_or_market(tz_name)

    # (when, magnitude, price, key, mult, is_buy) — SnapTrade reports SELL units
    # negative; take the magnitude and drive side off the activity type.
    events: list[tuple[datetime, Decimal, Decimal, tuple, Decimal, bool]] = []
    for act in activities:
        typ = str(_attr(act, "type", default="")).upper()
        if typ not in ("BUY", "SELL"):
            continue  # skip dividends/fees/exercises — realized from trades only
        units = _dec(_attr(act, "units"))
        price = _dec(_attr(act, "price"))
        when_raw = _attr(act, "trade_date", "settlement_date")
        if units is None or price is None or not when_raw:
            continue
        try:
            when = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        key, mult = _contract(act)
        events.append((when, abs(units), price, key, mult, typ == "BUY"))

    events.sort(key=lambda e: e[0])

    lots: dict[tuple, deque] = defaultdict(deque)  # key -> deque[[qty(signed), price]]
    daily: dict[date, tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))
    for when, mag, price, key, mult, is_buy in events:
        day = when.astimezone(bucket_tz).date()
        # Walk pre-range fills too (day=None) so cost basis stays correct, but
        # only record P&L for days inside [start, end].
        _apply(lots, key, mag, price, mult, is_buy, daily,
               day if start <= day <= end else None)

    return dict(daily)


def _apply(lots, key, qty, price, mult, is_buy, daily, day):
    """FIFO one trade into `lots`, crediting realized P&L to daily[day] (unless
    day is None — a pre-range fill we walk only to keep cost basis correct)."""
    q = qty
    if is_buy:
        # close shorts first, else open/extend a long
        if lots[key] and lots[key][0][0] < 0:
            pnl = Decimal(0)
            while q > 0 and lots[key] and lots[key][0][0] < 0:
                lot = lots[key][0]
                take = min(q, -lot[0])
                pnl += (lot[1] - price) * take * mult
                lot[0] += take
                q -= take
                if lot[0] == 0:
                    lots[key].popleft()
            if day is not None and pnl != 0:
                cur, n = daily[day]
                daily[day] = (cur + pnl, n + 1)
            if q > 0:
                lots[key].append([q, price])
        else:
            lots[key].append([q, price])
    else:
        # close longs first, else open/extend a short
        if lots[key] and lots[key][0][0] > 0:
            pnl = Decimal(0)
            while q > 0 and lots[key] and lots[key][0][0] > 0:
                lot = lots[key][0]
                take = min(q, lot[0])
                pnl += (price - lot[1]) * take * mult
                lot[0] -= take
                q -= take
                if lot[0] == 0:
                    lots[key].popleft()
            if day is not None and pnl != 0:
                cur, n = daily[day]
                daily[day] = (cur + pnl, n + 1)
            if q > 0:
                lots[key].append([-q, price])
        else:
            lots[key].append([-q, price])
