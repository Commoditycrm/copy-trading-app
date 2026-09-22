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


def consume_app_originated(order_id: uuid.UUID) -> bool:
    """Atomically read-and-clear the marker. True if it was set.

    Single-use on purpose. :func:`adopt_app_placed_order` claims a row with it,
    and two feed rows must never claim the SAME order — that would hide a
    genuine second trade behind the first."""
    try:
        return bool(get_sync_redis().getdel(_key(order_id)))
    except Exception:  # noqa: BLE001
        log.warning(
            "order_intent: failed to consume app-originated marker for order=%s",
            order_id, exc_info=True,
        )
        return False


def adopt_app_placed_order(
    db, user_id, broker_order_id: str, *,
    symbol: str, side, quantity, instrument_type,
):
    """Re-attach a broker feed row to the order OUR APP placed, instead of
    inserting a duplicate. Returns the adopted Order, or None.

    Why this exists, separately from :func:`is_app_originated`
    ----------------------------------------------------------
    The marker guard needs the broker to echo our ``client_order_id`` back.
    Alpaca, Webull and IBKR do. **SnapTrade has no client-order-id concept at
    all** — the adapter never sends one and ``AccountOrderRecord`` has no field
    for it — so on SnapTrade a listener cannot tell "our order" from "an
    external order" by identifier.

    It cannot fall back to the broker id either, because that is the thing that
    drifts: SnapTrade files an order under a DIFFERENT id than it returned at
    placement (see _relink_orphaned_mirror_ids, "prod: 49 stuck mirrors"). So
    the listener's ``WHERE broker_order_id = …`` lookup misses and it inserts a
    second parent row — which then fans out AGAIN, giving every subscriber two
    mirrors for one trader trade.

    So match on what we DO know: our own recent app-placed orders. The Redis
    marker is keyed on our Order id, which needs nothing from the broker. We
    look for a marked order with the same instrument, side and quantity placed
    inside the marker TTL, and adopt the feed's id onto it.

    Adopting rather than skipping matters for a TRADER: a trader genuinely does
    place orders outside our app, and those must still be recorded and mirrored.
    Skipping would silently drop them. Adopting only ever RE-LABELS a row we
    already created, so an unmatched external order still falls through to the
    normal insert path.

    It also repairs the instrument: the row we placed carries the real contract
    (we chose the strike and expiry), whereas a row rebuilt from the feed can
    come back typed STOCK when the payload omits the option block — which drops
    the ×100 contract multiplier from realized P&L and splits the round trip
    across two FIFO buckets.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.models.order import Order  # noqa: PLC0415

    if not broker_order_id or quantity is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_TTL_S)
    candidates = db.execute(
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.parent_order_id.is_(None),
            Order.symbol == symbol,
            Order.side == side,
            Order.instrument_type == instrument_type,
            Order.quantity == quantity,
            Order.created_at >= cutoff,
            Order.broker_order_id != broker_order_id,
        )
        .order_by(Order.created_at.desc())
        .limit(5)
    ).scalars().all()
    for cand in candidates:
        # consume => single-use, so a second feed row can't claim it too.
        if consume_app_originated(cand.id):
            log.info(
                "order_intent: adopting broker order %s onto app-placed order %s "
                "(%s %s x%s) instead of inserting a duplicate",
                broker_order_id, cand.id, side, symbol, quantity,
            )
            cand.broker_order_id = broker_order_id
            return cand
    return None


__all__ = [
    "mark_app_originated",
    "is_app_originated",
    "consume_app_originated",
    "adopt_app_placed_order",
]
