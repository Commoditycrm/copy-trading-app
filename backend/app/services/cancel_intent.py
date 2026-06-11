"""Short-lived "no-cascade" markers for cancel-all-open with
include_subscribers=false.

Problem this solves
-------------------
The trade endpoint can cancel a trader's order WITHOUT cascading to
subscriber mirrors (the "Cancel My Orders" UX). It just skips the
cascade call. But the broker listener (Alpaca WebSocket / SnapTrade
poller / IBKR ZMQ) doesn't know that — it sees the broker fire a
``canceled`` event for a trader root order that's marked
``fanned_out_to_subscribers=True`` and runs its OWN cascade as if the
cancel had come from outside our app (broker UI, broker mobile app,
direct API call). That double-cascade is exactly the wrong behavior
for the "just me" path: subscribers' mirrors get cancelled even though
the trader explicitly said don't.

Approach
--------
When the trade endpoint cancels a trader root order with
``include_subscribers=false``, it calls :func:`mark_no_cascade` to set
a Redis flag keyed to the order id. The listener checks the flag (and
consumes it) via :func:`consume_no_cascade` BEFORE running its
cascade. If the flag is present, the cascade is skipped.

TTL is short (5 minutes by default) — long enough to outlive any
realistic broker → listener delivery delay, short enough that a
forgotten / orphaned marker can't suppress a legitimate cascade from a
later, unrelated event on the same order id (which can't really happen
since the order is already terminal, but defence in depth).

Stateless / no DB schema change — the entire mechanism is a single
Redis key per order. Failures (Redis down) fall back to the
listener's prior behavior (cascade happens), so the worst case is the
pre-fix bug — never a missed cancel.
"""
from __future__ import annotations

import logging
import uuid

from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

# Marker TTL — generous because broker → listener delivery can be slow
# during reconnect / replay, and the marker is auto-deleted on consume
# anyway. Tightening this isn't worth the complexity.
_NO_CASCADE_TTL_S = 300

_KEY_PREFIX = "cancel:no_cascade:"


def _key(order_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{order_id}"


def mark_no_cascade(order_id: uuid.UUID) -> None:
    """Tell the listeners: when you see the terminal event for this
    order, DON'T cascade to subscriber mirrors.

    Best-effort — Redis errors are swallowed and logged. If the marker
    isn't set, the listener falls back to its default cascade behavior
    (which matches the bug we're fixing, but at least nothing crashes).
    """
    try:
        get_sync_redis().setex(_key(order_id), _NO_CASCADE_TTL_S, "1")
    except Exception:  # noqa: BLE001
        log.warning(
            "cancel_intent: failed to set no-cascade marker for order=%s",
            order_id, exc_info=True,
        )


def consume_no_cascade(order_id: uuid.UUID) -> bool:
    """Called by listeners before they run a cancel-cascade.

    Returns True if a no-cascade marker existed for this order id (and
    deletes it so a second observation of the same terminal event
    doesn't keep blocking). Returns False on any failure — failing
    open is safer than failing closed, since failing closed would
    suppress legitimate cascades.

    Uses GETDEL where available (Redis 6.2+) to make read-then-delete
    atomic. Falls back to a non-atomic GET+DELETE on older Redis,
    which has a tiny double-consume window that's harmless here (both
    consumers would suppress the cascade, which is what we want anyway).
    """
    try:
        client = get_sync_redis()
        # Prefer the atomic getdel; fall back if the server is too old.
        try:
            value = client.getdel(_key(order_id))
        except (AttributeError, Exception):  # noqa: BLE001
            value = client.get(_key(order_id))
            if value is not None:
                client.delete(_key(order_id))
        return value is not None
    except Exception:  # noqa: BLE001
        log.warning(
            "cancel_intent: failed to read no-cascade marker for order=%s "
            "— defaulting to cascade",
            order_id, exc_info=True,
        )
        return False


__all__ = ["mark_no_cascade", "consume_no_cascade"]
