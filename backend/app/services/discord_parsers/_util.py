"""Shared readers for the fiddly bits every alert format gets wrong."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

# Strikes carry thousands separators and decimals in the wild — $1,015 / $7,700 /
# $227.50 all appear in real feeds. A naive \d+ reads "$1,015" as 1, which would
# buy a completely different contract at a plausible-looking price. Always match
# the whole number including separators.
_NUMBER = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"

MONEY_RE = re.compile(rf"\$?\s*({_NUMBER})")


def to_decimal(raw: str | None) -> Decimal | None:
    """Parse a number that may carry $ and thousands separators."""
    if raw is None:
        return None
    cleaned = str(raw).replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_expiry(
    raw: str, *, posted_at: datetime | None, today: date | None = None
) -> tuple[date | None, str | None]:
    """Read an expiry, returning ``(date, error)``.

    Handles ``09/11``, ``09/11/26``, ``2026-09-11``, ``SEP18``, ``18 SEP``.

    The hard case is a bare ``MM/DD`` with no year. We resolve it against the
    date the alert was POSTED, not today: an option expires on or after the day
    it was alerted, so the year is the first one that puts the expiry on or after
    that date. Resolving against "now" instead would silently roll a January
    expiry into next year when re-reading an old message.

    Without a posted date the year is genuinely unknowable, so we refuse rather
    than assume — a wrong year is a wrong contract.
    """
    text = (raw or "").strip().upper()
    if not text:
        return None, "no expiry in the alert"

    # ISO: 2026-09-11
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # MM/DD/YY or MM/DD/YYYY
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})", text)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return _safe_date(year, int(m.group(1)), int(m.group(2)))

    # MON D / MON DD / D MON  (SEP18, SEP 18, 18 SEP)
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    m = re.fullmatch(r"([A-Z]{3,4})\s*(\d{1,2})", text) or re.fullmatch(
        r"(\d{1,2})\s*([A-Z]{3,4})", text
    )
    if m:
        a, b = m.group(1), m.group(2)
        mon_name, day = (a, b) if a.isalpha() else (b, a)
        month = months.get(mon_name)
        if month:
            return _resolve_year(month, int(day), posted_at=posted_at, today=today)

    # MM/DD with no year — the common case, resolved against the posted date.
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if m:
        return _resolve_year(int(m.group(1)), int(m.group(2)), posted_at=posted_at, today=today)

    return None, f"couldn't read the expiry {raw!r}"


def _resolve_year(
    month: int, day: int, *, posted_at: datetime | None, today: date | None
) -> tuple[date | None, str | None]:
    ref = posted_at.date() if posted_at else today
    if ref is None:
        return None, "expiry has no year and the alert has no timestamp to resolve it against"

    candidate, err = _safe_date(ref.year, month, day)
    if err:
        return None, err
    if candidate is None:
        return None, "couldn't resolve the expiry"

    # Only roll a bare MM/DD into next year when it's SO far behind the alert
    # that a year boundary is the only sane reading (posted late December,
    # expiring 01/02).
    #
    # A date a few days or weeks past is far more likely a stale or mistyped
    # alert. Rolling that forward a year would silently produce a real, tradeable
    # contract twelve months out — a wrong trade that looks right. Keeping the
    # past date instead yields an EXPIRED contract, which downstream validation
    # rejects loudly. When the reading is ambiguous, prefer the one that fails
    # visibly over the one that quietly trades.
    if candidate < ref - timedelta(days=180):
        return _safe_date(ref.year + 1, month, day)
    return candidate, None


def _safe_date(year: int, month: int, day: int) -> tuple[date | None, str | None]:
    try:
        return date(year, month, day), None
    except ValueError:
        return None, f"{year}-{month:02d}-{day:02d} isn't a real date"
