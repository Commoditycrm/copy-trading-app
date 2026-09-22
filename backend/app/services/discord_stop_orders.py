"""Keep a real STOP order resting at the broker for each protected position.

The trim ladder decides a stop LEVEL. This puts an actual order at that level
instead of watching the price ourselves.

Why that matters: an emulated stop only exists while our poller runs. If the
backend is down, restarting, or wedged, nothing is protecting the position — and
that is exactly when a trader would most want the stop to work. A resting order
lives at Alpaca and fires whether we are up or not.

Alpaca accepts ``stop`` on single-leg options (market and limit too; only
trailing stops are refused there), so the level the ladder sets can be a genuine
order. The TRAILING exits on rungs 2 and 3 still have to be emulated — there is
no native option trailing stop to hand them to.

── Reconciled, not fired-and-forgotten ─────────────────────────────────────────
Each tick compares what SHOULD rest (the guard's level, against the quantity
actually held) with what DOES rest, and fixes the difference. That is what makes
it survive the things that break a place-once approach: a partial fill, a trim
that changed the size, a stop cancelled by hand at the broker, or a restart
midway through.

One subtlety worth stating: a resting SELL stop RESERVES those contracts. If a
trim then tries to sell them, the broker rejects it for insufficient quantity —
so the stop is sized to what is NOT already earmarked for a trailing exit, and
callers cancel it before placing an exit of their own.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.services import discord_position_guard as guards

log = logging.getLogger(__name__)


def desired_quantity(held: Decimal, guard) -> Decimal:
    """How many contracts the resting stop should cover.

    Everything held, minus any slice already earmarked to leave on a trailing
    exit. Covering those too would reserve them, and the trailing exit would be
    rejected for insufficient quantity when it fires.
    """
    earmarked = Decimal(str(guard.trail_qty or 0))
    return max(Decimal(0), held - earmarked)


def reconcile(db: Session, guard, held: Decimal, place_stop, cancel_stop) -> str:
    """Make the broker match the guard. Returns a short outcome for logging.

    ``place_stop(quantity, stop_price) -> order_id`` and ``cancel_stop(order_id)``
    are injected so this module never talks to a broker itself — the caller owns
    that and routes it through the same path every other order takes.
    """
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    want_qty = desired_quantity(held, guard)
    want_price = guard.stop_price

    resting = db.get(Order, guard.stop_order_id) if guard.stop_order_id else None
    # A filled or cancelled order is not resting, whatever the guard remembers.
    if resting is not None and resting.status not in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    ):
        guard.stop_order_id = None
        resting = None

    # Nothing to protect: no level set, or the position is gone or fully
    # earmarked. Pull any order we left behind.
    if want_price is None or want_qty <= 0:
        if resting is not None:
            cancel_stop(resting.id)
            guard.stop_order_id = None
            return "cancelled (nothing to protect)"
        return "idle"

    if resting is None:
        # A REJECTED stop is not resting, so without this the reconciler would
        # place a fresh one every tick and keep collecting the same rejection —
        # 29 of them, in the case that led to this guard. Back off instead and
        # let the cause be fixed.
        if _recently_rejected(db, guard):
            return "backing off (recent rejection)"
        oid = place_stop(want_qty, want_price)
        guard.stop_order_id = oid
        log.info(
            "discord stop: resting SELL %s %s STOP @ %s at the broker",
            want_qty, guard.symbol, want_price,
        )
        return f"placed {want_qty} @ {want_price}"

    same_qty = Decimal(str(resting.quantity)) == want_qty
    same_price = resting.stop_price is not None and Decimal(str(resting.stop_price)) == want_price
    if same_qty and same_price:
        return "in sync"

    # The ladder moved the level, or a fill changed the size. Replace it.
    cancel_stop(resting.id)
    oid = place_stop(want_qty, want_price)
    guard.stop_order_id = oid
    log.info(
        "discord stop: %s stop moved to %s x%s (was %s x%s)",
        guard.symbol, want_price, want_qty, resting.stop_price, resting.quantity,
    )
    return f"replaced -> {want_qty} @ {want_price}"


# How long to wait after a rejected stop before trying again. Long enough that a
# persistent cause (a bad price, a contract the broker won't take a stop on)
# produces a handful of orders a day rather than one every tick.
_REJECT_BACKOFF_S = 900


def _recently_rejected(db: Session, guard) -> bool:
    """Whether a stop for this contract was rejected in the last backoff window."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from app.models.order import Order, OrderStatus, OrderType  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    # Never look further back than THIS guard. The window is per contract, and a
    # guard is per position, so a rejection belonging to a position that has
    # since closed would otherwise keep the NEXT one unprotected for the rest of
    # the window -- live, a fresh NIO entry went 15 minutes with no stop because
    # the previous position's stop had been refused.
    since = datetime.now(timezone.utc) - timedelta(seconds=_REJECT_BACKOFF_S)
    created = getattr(guard, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        since = max(since, created)
    hit = db.execute(
        select(Order.id).where(
            Order.user_id == guard.user_id,
            Order.symbol == guard.symbol,
            Order.option_strike.is_not_distinct_from(guard.option_strike),
            Order.option_expiry.is_not_distinct_from(guard.option_expiry),
            Order.order_type == OrderType.STOP,
            Order.status == OrderStatus.REJECTED,
            Order.created_at >= since,
        ).limit(1)
    ).scalar_one_or_none()
    return hit is not None


def release(db: Session, guard, cancel_stop) -> bool:
    """Cancel the resting stop so its contracts are free to be sold.

    Called before a trim or close places its own order: the broker reserves the
    contracts under a resting stop and would otherwise reject the exit for
    insufficient quantity. The next reconcile re-places a correctly sized stop
    on whatever is left.
    """
    if not guard.stop_order_id:
        return False
    try:
        cancel_stop(guard.stop_order_id)
    except Exception:  # noqa: BLE001
        log.warning("discord stop: could not release %s", guard.symbol, exc_info=True)
        return False
    guard.stop_order_id = None
    log.info("discord stop: released %s's resting stop to make room for an exit", guard.symbol)
    return True


__all__ = ["desired_quantity", "reconcile", "release"]
