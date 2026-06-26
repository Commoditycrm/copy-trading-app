"""Short-lived "app-originated" markers for orders we placed ourselves.

Problem this solves
-------------------
When a trader places an order through our Trade Panel, ``api/trades.py``
creates the parent Order row and fans it out. The broker listener
(Alpaca WebSocket) ALSO sees that same order on the trade_updates stream
and, if it doesn't yet recognise it, treats it as an externally-placed
trade — inserting a SECOND parent and fanning out AGAIN. Result: the
subscriber gets two mirror orders for one trade (the "doubling" bug).

The listener already dedupes by ``broker_order_id``, but that only works
once our row is committed WITH the broker id set. There's a race window:
the broker emits the WS event the instant it accepts the order, which can
reach the listener before ``api/trades.py`` has committed (its row is
still uncommitted and its ``broker_order_id`` not yet assigned). In that
window the listener's lookup misses and it creates the duplicate.

Approach
--------
Before it calls the broker, ``api/trades.py`` marks the order id with
:func:`mark_app_originated`. We pass that same id to the broker as
``client_order_id``, so the listener gets it back on every event. When the
listener is about to treat an order as externally-placed, it checks
:func:`is_app_originated` first; if set, our app owns the order's creation
and fanout, so the listener skips it. Once our row commits, the listener's
normal ``broker_order_id`` dedup takes over for subsequent events.

TTL is short — long enough to outlive the broker -> listener delivery race
(milliseconds in practice), short enough that a forgotten marker can't
suppress a genuinely external order that happens to reuse the id (which
can't really happen — the id is our own UUID).

Stateless / no schema change — one Redis key per order. On Redis failure
the listener falls back to its prior behavior (broker_order_id dedup),
so the worst case is the pre-fix race, never a crash or a missed mirror.
"""
from __future__ import annotations

import logging
import uuid

from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

# Generous enough to cover a slow broker -> listener round-trip on a
# congested connection; the race it guards is normally sub-second.
_TTL_S = 120

_KEY_PREFIX = "order:app_originated:"


def _key(order_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{order_id}"


def mark_app_originated(order_id: uuid.UUID) -> None:
    """Record that THIS app placed the order with this id — the listener
    should not re-detect it as an external trade. Best-effort."""
    try:
        get_sync_redis().setex(_key(order_id), _TTL_S, "1")
    except Exception:  # noqa: BLE001
        log.warning(
            "order_intent: failed to set app-originated marker for order=%s",
            order_id, exc_info=True,
        )


def is_app_originated(order_id: uuid.UUID) -> bool:
    """True if our app placed this order (marker still live). Returns False
    on any failure — failing open keeps the listener's broker_order_id
    dedup as the backstop rather than dropping a legitimate external order."""
    try:
        return get_sync_redis().get(_key(order_id)) is not None
    except Exception:  # noqa: BLE001
        log.warning(
            "order_intent: failed to read app-originated marker for order=%s "
            "— treating as not-ours",
            order_id, exc_info=True,
        )
        return False


__all__ = ["mark_app_originated", "is_app_originated"]
