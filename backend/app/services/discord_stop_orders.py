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
    # …and minus the slice a resting take-profit holds (discord_take_profit):
    # that slice carries its own linked stop, and a second sell on the same
    # contracts would be refused.
    earmarked += Decimal(str(getattr(guard, "tp_qty", None) or 0))
    return max(Decimal(0), held - earmarked)


# Rejections that are about the CONNECTION, not the order. Retrying these is
# obviously right; closing a position over a rate limit is obviously wrong.
# Everything else is treated as "this stop will not rest" — see _place_or_close.
#
# PHRASES ONLY — never bare status numbers. _place_trader_order wraps every
# broker rejection as HTTPException(502, ...), whose str() begins "502: ", so a
# "502" marker matched EVERY rejection and this fallback could never fire once.
# A RequestID (a UUID) rides along in these messages too, and would sooner or
# later contain any three-digit code as a substring.
_TRANSIENT_MARKERS = (
    "too many requests", "rate limit", "throttl", "quota",
    "timeout", "timed out", "deadline exceeded",
    "connection reset", "connection aborted", "connection refused",
    "temporarily unavailable", "service unavailable", "gateway timeout",
    "unauthorized", "invalid token", "token expired",
)


# Rejections that mean "these contracts are still held by another resting sell"
# — not that the market is through the stop. Webull reads a sell for more than
# is free as OPENING a short and refuses it as a naked call; Alpaca calls it a
# cash-secured put it can't fund. It happens when a stop or take-profit is
# replaced and the broker has not finished releasing the old one. QA 2026-10-06:
# moving SPY 779C's stop from 0.75 to 1.01 was refused this way while the 0.75
# stop still held all 5, and the refusal was read as "the stop would have
# fired" — the position was sold at market, below every trim.
_CONFLICT_MARKERS = (
    "naked call", "naked put", "net moeny not enough", "net money not enough",
    "new naked", "cash-secured", "cash secured", "insufficient", "intent mismatch",
    "position intent", "exceeds", "not enough position", "available quantity",
)


def is_reservation_conflict(msg: str) -> bool:
    """The broker refused a sell because the contracts are still reserved by
    another resting order. A wait, never a reason to close the position."""
    m = str(msg or "").lower().replace("_", " ")
    return any(k in m for k in _CONFLICT_MARKERS)


# A refusal for held-up contracts is retried once the old order has had time
# to go — not every pass, in case something else is holding them.
_CONFLICT_RETRY_S = 45


def _is_transient(msg: str) -> bool:
    # Underscores normalised so a broker's own code name matches the phrase:
    # Webull says TOO_MANY_REQUESTS, not "too many requests".
    m = (msg or "").lower().replace("_", " ")
    return any(k in m for k in _TRANSIENT_MARKERS)


def _place_or_close(db, guard, quantity, price, place_stop, close_position) -> str | None:
    """Place the stop; if the broker refuses it, exit the position instead.

    A refused stop leaves the position unprotected, and the most common refusal
    says so outright: Webull answers

      OPENAPI_STOP_PRICE_MUST_BE_LESS_THAN_MARKET_PRICE
      "Stop price must be less than market price for a sell order (0.21)"

    which means the market is ALREADY at or through the level — the stop we
    asked for would have fired the moment it rested. Closing now is what the
    stop was for, not an escalation of it.

    The ladder cannot prevent this by checking the price first: it validates the
    level against the mark when the ALERT arrives, but the order is placed by the
    poller up to a minute later (Webull's quota allows one sweep a minute), and
    the market moves in between. Live: a break-even stop at 0.24 was set while
    the mark was ~0.30 and refused at 0.21.

    Returns the placed order id, or None when the position was closed instead.
    Transient failures re-raise so the existing backoff handles them — a stop
    refused by a rate limit is not a stop the broker disagrees with.
    """
    try:
        return place_stop(quantity, price)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if close_position is None or _is_transient(msg) or is_reservation_conflict(msg):
            # Retried on a later pass. The emulated stop covers the position
            # meanwhile (discord_trailing_stop.decide: no stop resting).
            raise
        log.warning(
            "discord stop: %s refused a stop at %s (%s) — closing %s instead, "
            "the position would otherwise be left unprotected",
            guard.symbol, price, msg[:160], quantity,
        )
        _exit_instead(db, guard, quantity, close_position)
        return None


def _exit_instead(db, guard, quantity, close_position) -> str:
    """Sell what the refused stop was meant to protect — and nothing more.

    The guard is NOT retired here. Retiring on submit is how a position ended up
    held with nothing managing it: live 2026-09-29, six refused-stop exits were
    sent after 16:00, expired unfilled, and their guards were already closed.
    The guard now stays live until the position is actually flat
    (``retire_if_flat``), so a failed exit is retried and a slice riding a
    trailing exit keeps being watched.
    """
    if guard.option_strike is not None:
        from app.services import market_hours  # noqa: PLC0415

        # Options only trade in the regular session; an exit sent outside it
        # expires, and sending one every tick all night is just noise.
        if not market_hours.in_regular_session():
            return "exit deferred to the regular session"
    if working_exit(db, guard) is not None:
        return "exit already working"
    from app.services.position_events import because  # noqa: PLC0415

    with because("the broker refused the stop — exited instead of holding it unprotected"):
        close_position(quantity)
    return "exit sent"


def reconcile(
    db: Session, guard, held: Decimal, place_stop, cancel_stop,
    close_position=None,
) -> str:
    """Make the broker match the guard. Returns a short outcome for logging.

    ``place_stop(quantity, stop_price) -> order_id`` and ``cancel_stop(order_id)``
    are injected so this module never talks to a broker itself — the caller owns
    that and routes it through the same path every other order takes.

    ``close_position(quantity)`` is the fallback when the broker will not hold
    the stop at all: an unprotected position is exited rather than left sitting.
    """
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    want_qty = desired_quantity(held, guard)
    want_price = guard.stop_price
    # A stop can't be $0 or less — a stop that rounded down to nothing is not a
    # stop, and placing it fails validation, which would read as a refusal.
    if want_price is not None and want_price <= 0:
        want_price = None

    resting = db.get(Order, guard.stop_order_id) if guard.stop_order_id else None
    # A filled or cancelled order is not resting, whatever the guard remembers.
    if resting is not None and resting.status not in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    ):
        if resting.status == OrderStatus.CANCELED:
            # Every cancel WE make (release before an exit, moving the level,
            # pulling a stop with nothing to protect) clears stop_order_id
            # first, so a stop the guard still points at that is now cancelled
            # was cancelled by the trader — "Cancel all open orders", Order
            # History, or the Alpaca dashboard. Honour it: forget the level, so
            # neither this reconciler nor the emulated enforcer brings it back.
            # Re-placing it was the old behaviour; live 2026-09-29 the trader
            # removed a ladder stop and watched it come back every tick, even
            # after turning Auto trim off. A later rung that sets a new level
            # places a new stop — that is a new decision, not this one undone.
            log.warning(
                "discord stop: %s's stop @ %s was cancelled outside the ladder "
                "— treating it as removed by the trader, not re-placing",
                guard.symbol, guard.stop_price,
            )
            from app.services.position_events import because  # noqa: PLC0415

            with because("cancelled outside the ladder (broker app, Order History or cancel all) — not put back"):
                guard.stop_order_id = None
                guard.stop_price = None
                if hasattr(db, "flush"):
                    db.flush()
            return "removed by the trader"
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

    # An exit is working for this contract (a trim, a manual close, a refused
    # stop's exit). It has the contracts reserved, so a stop placed now would be
    # refused for insufficient quantity — and a refusal triggers another exit.
    # Wait for it to finish; the next tick sizes the stop to what is left.
    if resting is None and working_exit(db, guard) is not None:
        return "waiting (exit working)"

    if resting is None:
        # A REJECTED stop is not resting, so without this the reconciler would
        # place a fresh one every tick and keep collecting the same rejection —
        # 29 of them, in the case that led to this guard. Back off instead and
        # let the cause be fixed.
        refusal = _recent_rejection_reason(db, guard)
        if refusal is not None and is_reservation_conflict(refusal):
            # The contracts were still held by the order being replaced. Not a
            # verdict on the stop: once that order has had time to go, place it.
            age = _recent_rejection_age_s(db, guard)
            if age is not None and age < _CONFLICT_RETRY_S:
                return "waiting (contracts still held by the order being replaced)"
            refusal = None
        if refusal is not None:
            # But "back off" cannot be the whole answer: we want a stop here,
            # the broker has refused one, and the position is open — so it is
            # sitting unprotected and waiting out the window only prolongs that.
            #
            # This is also what makes the rescue reliable. Closing from the
            # exception alone only works on the single tick that happens to
            # place the order; if anything goes wrong on that tick the backoff
            # then short-circuits every later one and the position stays
            # unprotected indefinitely. Reading the persisted rejection means
            # any subsequent tick can still act.
            if close_position is not None and not _is_transient(refusal):
                log.warning(
                    "discord stop: %s has no stop resting and the broker refused "
                    "the last one (%s) — closing %s rather than leaving it open",
                    guard.symbol, refusal[:160], want_qty,
                )
                return _exit_instead(db, guard, want_qty, close_position)
            return "backing off (recent rejection)"
        oid = _place_or_close(
            db, guard, want_qty, want_price, place_stop, close_position
        )
        if oid is None:
            return "closed (stop refused)"
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

    # The ladder moved the level, or a fill changed the size. Replace it — in
    # two passes. The old stop is cancelled now and the new one placed on the
    # NEXT pass: a broker releases the contracts a cancelled order held only
    # once the cancel completes, and a new stop placed in the same instant asks
    # for contracts that are still taken. Webull refuses that as opening a
    # naked call (QA 2026-10-06, SPY 779C). Until the next pass the emulated
    # stop covers the position: with nothing resting, discord_trailing_stop
    # enforces the level itself.
    cancel_stop(resting.id)
    guard.stop_order_id = None
    log.info(
        "discord stop: %s stop moving to %s x%s (was %s x%s) — placed next pass",
        guard.symbol, want_price, want_qty, resting.stop_price, resting.quantity,
    )
    return f"cancelled {resting.quantity} @ {resting.stop_price}; {want_qty} @ {want_price} next pass"


# How long to wait after a rejected stop before trying again. Long enough that a
# persistent cause (a bad price, a contract the broker won't take a stop on)
# produces a handful of orders a day rather than one every tick.
_REJECT_BACKOFF_S = 900


def _recently_rejected(db: Session, guard) -> bool:
    """Whether a stop for this contract was rejected in the last backoff window."""
    return _recent_rejection_reason(db, guard) is not None


def _recent_rejection_age_s(db: Session, guard) -> float | None:
    """Seconds since the most recent refused stop for this contract, or None."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from app.models.order import Order, OrderStatus, OrderType  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    at = db.execute(
        select(Order.created_at).where(
            Order.user_id == guard.user_id,
            Order.symbol == guard.symbol,
            Order.option_strike.is_not_distinct_from(guard.option_strike),
            Order.option_expiry.is_not_distinct_from(guard.option_expiry),
            Order.order_type == OrderType.STOP,
            Order.status == OrderStatus.REJECTED,
        ).order_by(Order.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


def _recent_rejection_reason(db: Session, guard) -> str | None:
    """The broker's reason for the most recent refused stop, or None.

    Read from the PERSISTED order rather than caught at the moment of failure.
    Catching it only works on the one tick that happens to place the order; this
    still knows on every later tick, which is what lets a position that is
    already sitting unprotected be rescued instead of waiting out the window.
    """
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
    row = db.execute(
        select(Order.reject_reason).where(
            Order.user_id == guard.user_id,
            Order.symbol == guard.symbol,
            Order.option_strike.is_not_distinct_from(guard.option_strike),
            Order.option_expiry.is_not_distinct_from(guard.option_expiry),
            Order.order_type == OrderType.STOP,
            Order.status == OrderStatus.REJECTED,
            Order.created_at >= since,
        ).order_by(Order.created_at.desc()).limit(1)
    ).one_or_none()
    if row is None:
        return None
    # A refusal we have no text for is still a refusal — report it as one, with
    # an empty reason. Returning None here would read as "never rejected" and
    # send the reconciler straight back to placing a stop every tick.
    return row[0] or ""


# How long a SELL may sit "working" before we stop trusting it. Order status is
# updated by the trade listener; a row it never heard back about must not block
# protection forever.
_WORKING_EXIT_MAX_AGE_S = 30 * 60


def working_exit(db: Session, guard):
    """A non-stop SELL for this contract still working at the broker, or None.

    Any exit counts — a ladder trim, a manual close, a refused stop's exit —
    because each reserves the contracts it is selling.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from app.models.order import Order, OrderSide, OrderStatus, OrderType  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    since = datetime.now(timezone.utc) - timedelta(seconds=_WORKING_EXIT_MAX_AGE_S)
    # The ladder's own resting take-profit is not an exit in progress: it sits
    # there by design, and its contracts are already kept out of the stop's size
    # (tp_qty). Counting it would stop every stop being placed while it rests.
    own_tp = getattr(guard, "tp_order_id", None)
    return db.execute(
        select(Order).where(
            Order.user_id == guard.user_id,
            *([Order.id != own_tp] if own_tp is not None else []),
            Order.parent_order_id.is_(None),
            Order.symbol == guard.symbol,
            Order.option_strike.is_not_distinct_from(guard.option_strike),
            Order.option_expiry.is_not_distinct_from(guard.option_expiry),
            Order.side == OrderSide.SELL,
            Order.order_type != OrderType.STOP,
            Order.status.in_([
                OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED, OrderStatus.RETRY_PENDING,
            ]),
            Order.created_at >= since,
        ).order_by(Order.created_at.desc()).limit(1)
    ).scalars().first()


def release_for_position(db: Session, user, pos) -> bool:
    """Free a ladder stop's contracts before a manual exit of ``pos``.

    A resting SELL stop reserves what it covers, so a manual close of the same
    contracts is read by Alpaca as OPENING a short and refused ("insufficient
    options buying power for cash-secured put") — live 2026-09-29, twice. The
    stop reconciler waits while that close works, then re-sizes the stop to
    whatever is left.
    """
    guard = guards.find(
        db, user.id, pos.symbol, pos.option_strike,
        getattr(pos, "option_right", None), pos.option_expiry,
    )
    if guard is None:
        return False
    from app.api.discord_sources import _cancel_stop_order  # noqa: PLC0415

    cancel = _cancel_stop_order(db, user)
    freed = False
    # A resting take-profit and its linked stop reserve contracts exactly as the
    # ladder stop does.
    if getattr(guard, "tp_order_id", None) is not None or getattr(guard, "tp_stop_order_id", None) is not None:
        from app.services import discord_take_profit  # noqa: PLC0415

        freed = discord_take_profit.release(db, guard, cancel)
        guard.tp_qty = None
    if not guard.stop_order_id:
        return freed
    return release(db, guard, cancel) or freed


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


__all__ = ["desired_quantity", "reconcile", "release", "release_for_position", "working_exit",
           "is_reservation_conflict"]
