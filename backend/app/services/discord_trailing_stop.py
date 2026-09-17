"""Enforces what a Discord trim left behind: a stop level, a trailing exit, or both.

Neither rests at the broker. Alpaca's options API rejects trailing stops
outright, and the codebase already emulates option stop-losses rather than
parking them as resting orders, so both live here and are advanced by the P&L
poller against live prices.

That has one consequence worth stating plainly: these stops only exist while the
poller is running. A native stop sits at the broker and survives our downtime;
these do not.

Two protections can be live on the same position at once, and they mean
different things:

  ``stop_price``  a hard floor under EVERYTHING still held. If it breaks, the
                  whole position leaves and the guard retires.
  ``trail_qty``   a slice already earmarked to leave, riding a trailing give-back
                  of ``trail_amount`` instead of having gone out at market.

The floor is checked first. If the position has broken its stop there is no
sense letting a slice keep riding — the trader wanted out below that price.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.services import discord_position_guard as guards

log = logging.getLogger(__name__)


def _key_of_position(p) -> tuple:
    return (
        (p.symbol or "").upper(),
        p.option_strike,
        (p.option_right.value if getattr(p.option_right, "value", None) else p.option_right),
        p.option_expiry,
    )


def _key_of_guard(g) -> tuple:
    return (
        (g.symbol or "").upper(),
        g.option_strike,
        (g.option_right.value if getattr(g.option_right, "value", None) else g.option_right),
        g.option_expiry,
    )


def _current_price(pos, user_id=None) -> Decimal | None:
    """The price this position is being judged at.

    A hand-pinned price wins when the feature is switched on, which is how the
    ladder gets tested without waiting for the market. It is off by default and
    must stay off in production — see services/price_override.
    """
    if user_id is not None:
        from app.services import price_override  # noqa: PLC0415
        pinned = price_override.apply_to(user_id, pos)
        if pinned is not None:
            log.info("discord stops: using pinned price %s for %s", pinned, pos.symbol)
            return pinned

    raw = getattr(pos, "current_price", None)
    if raw is None:
        return None
    try:
        price = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return price if price > 0 else None


def enforce(db: Session, user_id, adapter, close_position) -> int:
    """Advance every protection this trader has live. Returns how many fired.

    ``close_position(position, guard, quantity)`` is injected so this module
    never places orders itself — the caller owns that, and routes it through the
    same path everything else uses.
    """
    rows = [g for g in guards.armed(db) if g.user_id == user_id]
    if not rows:
        return 0

    try:
        positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        # A failed read is not a reason to exit anything. Skip the tick.
        log.warning("discord stops: position read failed for user=%s", user_id, exc_info=True)
        return 0

    by_contract = {_key_of_position(p): p for p in positions}
    fired = 0

    for guard in rows:
        pos = by_contract.get(_key_of_guard(guard))
        if pos is None:
            # Position gone — closed by hand, expired, or stopped out elsewhere.
            guards.retire(db, guard, "position no longer held")
            continue

        price = _current_price(pos, user_id)
        if price is None:
            continue    # no usable mark this tick

        held = abs(Decimal(str(pos.quantity)))
        if held <= 0:
            guards.retire(db, guard, "position no longer held")
            continue

        # ── the floor, first ────────────────────────────────────────────────
        # Skipped when a real order rests at the broker for this level: it will
        # fire on its own, and enforcing here as well would sell the same
        # contracts twice.
        stop = None if guard.stop_order_id else guard.stop_price
        if stop is not None and price <= stop:
            log.info("discord stops: %s at %s broke its %s stop — closing %s",
                     guard.symbol, price, stop, held)
            try:
                close_position(pos, guard, held)
            except Exception:  # noqa: BLE001
                # Leave it armed so the next tick tries again — an exit that
                # failed once must not be forgotten.
                log.exception("discord stops: stop-out failed for %s", guard.symbol)
                continue
            guards.retire(db, guard, f"stop hit at {price} (stop {stop})")
            fired += 1
            continue

        # ── then the trailing slice ─────────────────────────────────────────
        qty = guard.trail_qty
        amount = guard.trail_amount
        if qty is None or amount is None or amount <= 0:
            continue

        peak = guard.peak_price
        if peak is None or price > peak:
            guard.peak_price = price
            continue    # a new high can't also be a give-back

        if price > peak - amount:
            continue    # still inside the trail

        sell = min(qty, held)
        log.info("discord stops: %s gave back %s from %s — trailing out %s",
                 guard.symbol, amount, peak, sell)
        try:
            close_position(pos, guard, sell)
        except Exception:  # noqa: BLE001
            log.exception("discord stops: trailing exit failed for %s", guard.symbol)
            continue

        guards.clear_trail(guard)
        fired += 1
        if sell >= held:
            guards.retire(db, guard, f"trailing exit filled at {price} (peak {peak})")

    return fired


__all__ = ["enforce"]
