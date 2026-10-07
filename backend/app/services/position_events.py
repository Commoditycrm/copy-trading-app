"""Record the history of a position's stop, for the Position summary.

A guard's stop (services/discord_position_guard) only ever holds its CURRENT
level: set by a trim, moved up by the next, raised by a trailing stop, removed
by hand. Orders and fills have rows of their own; this is the part that didn't.

Rather than remember to log at every place a stop changes — the ladder, auto
trim, take-profit fills, the trailing ratchet, the Positions page, the stop
reconciler — one SQLAlchemy ``before_flush`` hook looks at every guard about to
be saved and writes a PositionEvent for each change it sees. Installed by the
app at start-up (``install()``), so the plain test sessions that build guards
without this table are unaffected.

Best-effort: a failure here is logged and the save goes ahead without the event.
"""
from __future__ import annotations

import logging
import weakref
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.discord_position_guard import DiscordPositionGuard
from app.models.position_event import PositionEvent

log = logging.getLogger(__name__)
_installed = False
# Per database: does it have the table? (A test database built table by table
# may not; recording into it would fail the whole flush.)
_has_table: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _ready(session: Session) -> bool:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine not in _has_table:
        try:
            _has_table[engine] = inspect(engine).has_table(PositionEvent.__tablename__)
        except Exception:  # noqa: BLE001
            _has_table[engine] = False
    return _has_table[engine]


def _change(guard, attr: str) -> tuple[bool, object, object]:
    """(changed, before, after) for one attribute in this flush."""
    hist = inspect(guard).attrs[attr].history
    if not hist.has_changes():
        return False, None, None
    before = hist.deleted[0] if hist.deleted else None
    after = hist.added[0] if hist.added else getattr(guard, attr)
    return True, before, after


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return Decimal(str(a)) == Decimal(str(b))


def events_for(guard: DiscordPositionGuard, now: datetime) -> list[PositionEvent]:
    """The events this guard's pending changes amount to."""
    out: list[PositionEvent] = []

    def ev(kind: str, **kw) -> None:
        out.append(PositionEvent(
            user_id=guard.user_id, symbol=(guard.symbol or "").upper(),
            option_strike=guard.option_strike, option_right=guard.option_right,
            option_expiry=guard.option_expiry, kind=kind, created_at=now, **kw,
        ))

    changed, before, after = _change(guard, "stop_price")
    if changed and not _same(before, after):
        trailing = getattr(guard, "stop_trail_pct", None) is not None
        common = {"trail_pct": guard.stop_trail_pct, "peak": guard.stop_peak} if trailing else {}
        if before is None:
            ev("trailing_stop_set" if trailing else "stop_set", price=after, **common)
        elif after is None:
            ev("stop_removed", old_price=before)
        else:
            raised = trailing and Decimal(str(after)) > Decimal(str(before))
            ev("trailing_stop_raised" if raised else "stop_moved", price=after, old_price=before, **common)

    changed, before, after = _change(guard, "trail_qty")
    if changed and not _same(before, after):
        if after is not None:
            peak = guard.peak_price
            level = (Decimal(str(peak)) - Decimal(str(guard.trail_amount))) \
                if peak is not None and guard.trail_amount is not None else None
            ev("trailing_exit_armed", quantity=after, trail_amount=guard.trail_amount,
               trail_pct=guard.trail_percent, peak=peak, price=level)
        else:
            ev("trailing_exit_cleared", quantity=before)
    return out


def _before_flush(session: Session, flush_context, instances) -> None:  # noqa: ANN001, ARG001
    try:
        guards = [o for o in list(session.new) + list(session.dirty) if isinstance(o, DiscordPositionGuard)]
        if not guards or not _ready(session):
            return
        now = datetime.now(timezone.utc)
        for obj in guards:
            for e in events_for(obj, now):
                session.add(e)
    except Exception:  # noqa: BLE001
        log.exception("position events: could not record a stop change")


def install() -> None:
    """Start recording. Idempotent."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    _installed = True


__all__ = ["install", "events_for"]
