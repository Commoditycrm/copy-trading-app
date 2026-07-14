"""US equity/option market-hours helpers (US Eastern, DST-aware).

Deliberately tiny and dependency-free (stdlib ``zoneinfo`` only) so any layer —
the copy-engine fanout, the EOD auto-close loop — can import it without pulling
in broker or DB code. Keeping every wall-clock decision around the US close in
ONE place guarantees the 15:55 auto-close sweep and the last-5-minutes order
lockout agree on exactly the same window.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# US equities/options trade on Eastern Time; ZoneInfo picks EST vs EDT per date.
ET = ZoneInfo("America/New_York")

# Regular-session close, and the 5-minute safety window that precedes it. The
# auto-close fires when we first cross EOD_WINDOW_START; new same-day-expiry
# subscriber orders are refused for the whole [EOD_WINDOW_START, MARKET_CLOSE).
MARKET_CLOSE = time(16, 0)
EOD_WINDOW_START = time(15, 55)


def now_et() -> datetime:
    """Current wall-clock in US Eastern (DST-aware)."""
    return datetime.now(ET)


def is_trading_weekday(dt_et: datetime | None = None) -> bool:
    """Mon–Fri. Does NOT know about market holidays or early-close days — but
    both callers tolerate that: on a closed/early day there's simply nothing to
    close (get_positions is empty) or the broker rejects the late order, so the
    worst case is a harmless no-op rather than a wrong action."""
    return (dt_et or now_et()).weekday() < 5


def in_eod_close_window(dt_et: datetime | None = None) -> bool:
    """True during the last 5 minutes before the US close (15:55–16:00 ET) on a
    weekday — the span in which we auto-close same-day-expiry positions and
    refuse new same-day-expiry orders."""
    dt = dt_et or now_et()
    return is_trading_weekday(dt) and EOD_WINDOW_START <= dt.time() < MARKET_CLOSE


def is_same_day_expiry(option_expiry: date | None, dt_et: datetime | None = None) -> bool:
    """True when an option expires on today's ET date (0DTE). False for stocks
    (``option_expiry is None``) and for any later-dated contract."""
    if option_expiry is None:
        return False
    return option_expiry == (dt_et or now_et()).date()
