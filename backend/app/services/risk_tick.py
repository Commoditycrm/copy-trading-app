"""Coalesce the LIVE Webull positions reads inside one risk-enforcement tick.

Each ``pnl_poller`` tick runs several independent sub-enforcers for the SAME
account — the option-SL monitor, the Discord stop reconcile, the emulated
trailing stops — and each one reads ``/openapi/assets/positions`` LIVE
(``cached_ok=False``) within ~0.2s of the others. Webull rejects the 3rd
near-simultaneous same-account read with 429, so one sub-enforcer was being
starved of positions (and silently skipping its check) ~38% of ticks.

This shares ONE fresh read across the read-only sub-enforcers of a tick while
keeping the risk path on live broker data:

  * the FIRST ``cached_ok=False`` read of the tick calls Webull (fresh);
  * later read-only sub-enforcers reuse that same fresh snapshot;
  * a ``place_order`` (the only path that leads to a holdings change) drops the
    snapshot, so the next sub-enforcer re-reads LIVE — no decision ever acts on
    a pre-mutation snapshot.

It is a no-op outside a tick (display/other callers keep their own behaviour)
and only the Webull adapter consults it — Alpaca is untouched. Scoping is via a
ContextVar, and ``pnl_poller`` runs each account's tick in its own thread/context
(``asyncio.to_thread`` copies the context), so ticks never share a snapshot.
"""
from __future__ import annotations

import contextvars
from typing import Any, Callable

_tick: contextvars.ContextVar["_Tick | None"] = contextvars.ContextVar(
    "risk_tick_positions", default=None
)


class _Tick:
    __slots__ = ("snapshots", "fresh_fetches", "reuses", "refreshes")

    def __init__(self) -> None:
        self.snapshots: dict[str, Any] = {}   # account key -> positions snapshot
        self.fresh_fetches = 0                 # live broker reads taken this tick
        self.reuses = 0                        # reads served from the tick snapshot
        self.refreshes = 0                     # snapshots dropped by a mutation


def begin() -> contextvars.Token:
    """Open a coalescing tick. Pass the returned token to ``end()``."""
    return _tick.set(_Tick())


def end(token: contextvars.Token) -> "_Tick | None":
    """Close the tick and return its counters (for instrumentation)."""
    t = _tick.get()
    _tick.reset(token)
    return t


def active() -> bool:
    return _tick.get() is not None


def get_or_fetch(key: str, fetch: Callable[[], Any]) -> Any:
    """Return this tick's fresh snapshot for ``key``, fetching once if absent.

    Outside a tick this just calls ``fetch()`` — behaviour is unchanged for
    display and other callers."""
    t = _tick.get()
    if t is None:
        return fetch()
    if key in t.snapshots:
        t.reuses += 1
        return t.snapshots[key]
    out = fetch()
    t.snapshots[key] = out
    t.fresh_fetches += 1
    return out


def invalidate(key: str | None = None) -> None:
    """Drop the cached snapshot after a broker-state mutation so the next read
    re-fetches LIVE. ``key=None`` clears every account in the tick. A no-op
    outside a tick, or when nothing is cached yet."""
    t = _tick.get()
    if t is None:
        return
    if key is None:
        if t.snapshots:
            t.snapshots.clear()
            t.refreshes += 1
    elif key in t.snapshots:
        del t.snapshots[key]
        t.refreshes += 1
