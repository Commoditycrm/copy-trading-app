"""Don't trade an entry alert that arrives late.

When the Discord listener comes back — a deploy, a restart, a dropped
connection — it replays every message posted while it was away, so that nothing
is silently lost (discord-listener/observer.js, RECONNECT). In auto mode each of
those used to be placed the moment it arrived: a QA restart bought the whole
backlog, at whatever the prices were by then.

So an ENTRY that reaches us more than ``discord_max_alert_age_s`` after it was
posted is not placed automatically. It is put back to PENDING with the reason,
and sits in the Discord tab for the trader to approve or reject — an alert
that is still worth taking can be taken with one click.

Exits are never held: a late trim or close still only reduces a position the
author has already left. Alerts the trader types into the composer are never
held either — they are as fresh as the click that sent them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.discord_message import SignalDecision

# Discord snowflakes count milliseconds from this instant, in the bits above 22.
_DISCORD_EPOCH_MS = 1_420_070_400_000


def posted_at(msg) -> datetime | None:
    """When the message was posted: Discord's own timestamp, else the time
    encoded in its snowflake id (always present on a real message)."""
    ts = getattr(msg, "posted_at", None)
    if ts is not None:
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    raw = str(getattr(msg, "discord_message_id", "") or "")
    if not raw.isdigit():
        return None
    ms = (int(raw) >> 22) + _DISCORD_EPOCH_MS
    if ms < _DISCORD_EPOCH_MS:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _is_entry(msg) -> bool:
    return str((getattr(msg, "parsed_signal", None) or {}).get("action") or "").upper() == "BUY"


def _typed_by_trader(msg) -> bool:
    author = getattr(msg, "author_id", None)
    return bool(author) and str(author) == str(getattr(msg, "user_id", None))


def hold_if_stale(msg, *, max_age_s: int | None = None, now: datetime | None = None) -> bool:
    """Take a late, auto-approved ENTRY back to PENDING instead of placing it.

    Returns True when it was held — the caller must then not execute it.
    """
    if getattr(msg, "decision", None) is not SignalDecision.APPROVED:
        return False
    if not _is_entry(msg) or _typed_by_trader(msg):
        return False
    when = posted_at(msg)
    if when is None:
        return False
    if max_age_s is None:
        from app.config import get_settings  # noqa: PLC0415

        max_age_s = get_settings().discord_max_alert_age_s
    now = now or datetime.now(timezone.utc)
    age = (now - when).total_seconds()
    if age <= max_age_s:
        return False
    msg.decision = SignalDecision.PENDING
    msg.decision_mode = "manual"
    msg.decided_at = None
    late = f"{round(age / 60)} min" if age >= 90 else f"{round(age)}s"
    msg.status_reason = (
        f"Not placed automatically — it was posted {late} before it reached Kopyya "
        "(caught up after a restart or reconnect). Approve it to place it now."
    )[:480]
    return True


__all__ = ["posted_at", "hold_if_stale"]
