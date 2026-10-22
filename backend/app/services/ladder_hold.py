"""Keep the Discord ladder's hands off a position while it is being closed by hand.

A manual close from Positions first releases the ladder's resting take-profit
and stop (they reserve the contracts), pauses for the cancels to settle, reads
positions again, and only then sends the close. The ladder's P&L poller pass
runs on its own clock in another process: in that gap it saw nothing resting
and no exit working, and put the take-profit and its linked stop straight
back — reserving the very contracts the close was about to sell. Webull then
refused the close ("will reverse an existing position … cancel an open
order"): QA 2026-10-22, CSCO 103P, a 3 @ 0.39 close.

So the close holds the position for its few seconds. The ladder pass skips a
held position; the close clears the hold the moment it has been sent —
accepted (the working close then keeps the ladder waiting, as it always has)
or refused (so protection goes straight back). The hold also expires on its
own, so a close that dies midway can never leave a position unprotected for
long.

Redis, so the web process (the close) and the worker (the ladder) agree.
Fail-open: no Redis means no hold, exactly the behaviour before.
"""
from __future__ import annotations

import contextvars
import functools
import logging

log = logging.getLogger(__name__)

MAX_HOLD_S = 20
_KEY = "ladder:hold:{}"


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


def hold(guard_id, seconds: int = MAX_HOLD_S) -> bool:
    """Hold this position's ladder. False when the hold couldn't be set."""
    if guard_id is None:
        return False
    try:
        _redis().set(_KEY.format(guard_id), "1", ex=int(seconds))
        return True
    except Exception:  # noqa: BLE001
        log.warning("ladder hold: could not hold %s", guard_id, exc_info=True)
        return False


def clear(guard_id) -> None:
    if guard_id is None:
        return
    try:
        _redis().delete(_KEY.format(guard_id))
    except Exception:  # noqa: BLE001
        log.warning("ladder hold: could not clear %s (it expires on its own)", guard_id, exc_info=True)


def held(guard_id) -> bool:
    if guard_id is None:
        return False
    try:
        return bool(_redis().exists(_KEY.format(guard_id)))
    except Exception:  # noqa: BLE001
        return False


# ── for a request: hold for its duration ───────────────────────────────────

_this_request: contextvars.ContextVar[list | None] = contextvars.ContextVar("ladder_holds", default=None)


def hold_for_request(guard_id) -> None:
    """Hold this position until the request running now ends (see
    ``released_after``); outside one, until the hold expires."""
    if hold(guard_id):
        held_here = _this_request.get()
        if held_here is not None:
            held_here.append(guard_id)


def released_after(fn):
    """Clear every hold ``fn`` took when it returns or raises. For a route:
    keeps the wrapped signature, which FastAPI reads."""
    @functools.wraps(fn)
    def _w(*args, **kwargs):
        token = _this_request.set([])
        try:
            return fn(*args, **kwargs)
        finally:
            for gid in _this_request.get() or []:
                clear(gid)
            _this_request.reset(token)
    return _w


__all__ = ["MAX_HOLD_S", "clear", "held", "hold", "hold_for_request", "released_after"]
