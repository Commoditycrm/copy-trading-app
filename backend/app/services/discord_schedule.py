"""When a Discord source should actually be watched.

A trader following US options alerts has no reason to hold a browser session
open at 3am. Restricting each source to an active window means fewer live
Discord sessions, less memory (a watcher is ~150-250MB of Chromium), and a
smaller automation footprint against Discord outside the hours that matter.

── How it's enforced ────────────────────────────────────────────────────────────
Nowhere in the listener. ``/internal/assignments`` simply omits a source that's
outside its window, and the listener's existing reconcile loop closes the watcher
because the assignment disappeared — the same path that handles a source being
disabled or deleted. When the window opens the assignment reappears and the
watcher starts. No new listener code, no scheduler, no cron.

That also means a missed sweep can only shift a boundary by one reconcile
interval (15s), never strand a watcher open or closed.

── Missed alerts are the real cost ──────────────────────────────────────────────
Anything posted while a source is outside its window is NOT ingested — we aren't
connected, so there is nothing to observe. That's the point of the feature, but
it's a genuine trade-off: a trader who narrows the window too far silently stops
seeing alerts. The default is ALWAYS, so existing sources are unaffected.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services import market_hours

log = logging.getLogger(__name__)

# Watch around the clock. The default, so a source created before this feature
# existed behaves exactly as it did.
ALWAYS = "always"
# 09:30-16:00 ET, Mon-Fri — the regular US cash session.
MARKET = "market"
# 04:00-20:00 ET, Mon-Fri — pre-market through post-market.
EXTENDED = "extended"
# A window the trader defines: start/end/timezone/days.
CUSTOM = "custom"

MODES = (ALWAYS, MARKET, EXTENDED, CUSTOM)

# Mon=0 … Sun=6, matching datetime.weekday().
WEEKDAYS = [0, 1, 2, 3, 4]


def _zone(name: str | None) -> ZoneInfo:
    """Resolve a timezone, falling back to ET rather than raising.

    A bad tz string must not take a source offline permanently — that would turn
    a typo into silently missed alerts, which is the worst failure this feature
    can have.
    """
    if not name:
        return market_hours.ET
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("discord_schedule: unknown timezone %r — falling back to ET", name)
        return market_hours.ET


def in_window(
    *,
    mode: str | None,
    start: time | None = None,
    end: time | None = None,
    timezone: str | None = None,
    days: list[int] | None = None,
    now: datetime | None = None,
) -> bool:
    """Should a source with this schedule be watched right now?

    Unknown modes and incomplete custom windows both resolve to True. Failing
    OPEN is deliberate: a misconfigured schedule should leave the trader
    over-watching, never silently not watching.
    """
    mode = (mode or ALWAYS).lower()

    if mode == ALWAYS:
        return True

    if mode == MARKET:
        dt = now.astimezone(market_hours.ET) if now else market_hours.now_et()
        return market_hours.in_regular_session(dt)

    if mode == EXTENDED:
        dt = now.astimezone(market_hours.ET) if now else market_hours.now_et()
        # Extended = pre + post, but the regular session sits between them and is
        # obviously included; in_extended_hours alone excludes it.
        return market_hours.in_regular_session(dt) or market_hours.in_extended_hours(dt)

    if mode == CUSTOM:
        if start is None or end is None:
            return True
        tz = _zone(timezone)
        dt = (now or datetime.now(tz)).astimezone(tz)
        allowed = days if days else WEEKDAYS
        t = dt.time()

        if start == end:
            # A zero-length window is meaningless; read it as "all day" rather
            # than "never", per the fail-open rule.
            return dt.weekday() in allowed
        if start < end:
            return dt.weekday() in allowed and start <= t < end
        # end < start → the window crosses midnight (e.g. 22:00-06:00). The DAY
        # check applies to the day the window STARTED, so the small-hours tail of
        # a Friday-night window still counts as Friday.
        if t >= start:
            return dt.weekday() in allowed
        prev_day = (dt.weekday() - 1) % 7
        return t < end and prev_day in allowed

    return True


def describe(
    *,
    mode: str | None,
    start: time | None = None,
    end: time | None = None,
    timezone: str | None = None,
) -> str:
    """One-line human summary for the UI and logs."""
    mode = (mode or ALWAYS).lower()
    if mode == MARKET:
        return "US market hours (09:30–16:00 ET, Mon–Fri)"
    if mode == EXTENDED:
        return "US extended hours (04:00–20:00 ET, Mon–Fri)"
    if mode == CUSTOM and start and end:
        tz = timezone or "America/New_York"
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} {tz}"
    return "Always"


__all__ = ["ALWAYS", "CUSTOM", "EXTENDED", "MARKET", "MODES", "WEEKDAYS", "describe", "in_window"]
