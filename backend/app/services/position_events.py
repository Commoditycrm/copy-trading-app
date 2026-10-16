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

WHY each change happened is set by the code path that makes it, with
``because("by you on Positions")`` around the work. Every event written while a
reason is active carries it as ``note`` — and so does every ORDER created in
that time (an ``order_note`` event joined to the order), which is how the
summary can say a sell was "Mark's alert: …", "auto-trim at +35%" or "stop
1.50 hit". ``because(…, keep_outer=True)`` leaves a reason already set in
place: auto-trim's reason survives the alert it submits.

Best-effort: a failure here is logged and the save goes ahead without the event.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
import weakref
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import Order
from app.models.position_event import PositionEvent

log = logging.getLogger(__name__)
_installed = False
_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar("position_event_reason", default=None)


@contextlib.contextmanager
def because(reason: str, *, keep_outer: bool = False):
    """Attribute the stop changes and orders made inside this block to ``reason``."""
    if keep_outer and _reason.get():
        yield
        return
    token = _reason.set((reason or "")[:300] or None)
    try:
        yield
    finally:
        _reason.reset(token)


def current_reason() -> str | None:
    return _reason.get()


def tagged(reason: str, *, keep_outer: bool = False):
    """Decorator form of ``because`` — for an API route or a loop's entry point.
    Keeps the signature (FastAPI reads the wrapped function's)."""
    import functools  # noqa: PLC0415
    import inspect as _inspect  # noqa: PLC0415

    def deco(fn):
        @functools.wraps(fn)
        def _w(*args, **kwargs):
            with because(reason, keep_outer=keep_outer):
                return fn(*args, **kwargs)
        # The route's annotations are strings (postponed); FastAPI would look
        # them up in THIS module. Resolve them now, in the route's own.
        _w.__signature__ = _inspect.signature(fn, eval_str=True)
        return _w
    return deco
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
            option_expiry=guard.option_expiry, kind=kind, created_at=now,
            note=_reason.get(), **kw,
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

    changed, before, after = _change(guard, "closed_at")
    if changed and before is None and after is not None:
        out.append(PositionEvent(
            user_id=guard.user_id, symbol=(guard.symbol or "").upper(),
            option_strike=guard.option_strike, option_right=guard.option_right,
            option_expiry=guard.option_expiry, kind="ladder_closed", created_at=now,
            note=(guard.closed_reason or _reason.get() or None),
        ))
    return out


def _order_note(order: Order, reason: str, now: datetime) -> PositionEvent:
    if order.id is None:
        order.id = uuid.uuid4()          # so the note can point at it
    right = getattr(order.option_right, "value", order.option_right)
    return PositionEvent(
        user_id=order.user_id, symbol=(order.symbol or "").upper(),
        option_strike=order.option_strike, option_right=right, option_expiry=order.option_expiry,
        kind="order_note", note=reason, order_id=order.id, quantity=order.quantity,
        created_at=now,
    )


def _before_flush(session: Session, flush_context, instances) -> None:  # noqa: ANN001, ARG001
    try:
        guards = [o for o in list(session.new) + list(session.dirty) if isinstance(o, DiscordPositionGuard)]
        reason = _reason.get()
        orders = [o for o in session.new if isinstance(o, Order)] if reason else []
        if not (guards or orders) or not _ready(session):
            return
        now = datetime.now(timezone.utc)
        for obj in guards:
            for e in events_for(obj, now):
                session.add(e)
        for order in orders:
            if order.user_id is not None and order.symbol:
                session.add(_order_note(order, reason, now))
    except Exception:  # noqa: BLE001
        log.exception("position events: could not record a stop change")


def install() -> None:
    """Start recording. Idempotent."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    _installed = True


__all__ = ["install", "events_for", "because", "current_reason", "tagged"]
