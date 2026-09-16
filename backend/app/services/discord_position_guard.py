"""What a SELL alert means depends on what came before it.

    BUY        → open the position, remember what it cost
    1st SELL   → only if up enough: sell half, stop the rest below entry
    2nd SELL   → sell half of what's left; that stop moves to break-even
    3rd SELL   → exit everything left

So an exit alert is not self-contained: the same message trims, protects or
exits depending on the position's history. That history lives in
``DiscordPositionGuard``, one row per open contract.

── Everything is measured from the ENTRY price ─────────────────────────────────
Not the live mark. A ladder keyed to the current price would move under itself:
each trim would reset the reference, so "25% below" would mean something
different on every rung and a falling position could ratchet its own stop down.
Keyed to entry, the levels are fixed the moment the position opens, and adding
to it later never moves a stop that is already protecting it.

── The trail is emulated for options ────────────────────────────────────────────
Alpaca's options API rejects trailing-stop orders (see trailing_stop_close.py),
and Discord alerts are almost entirely options. So arming a trail records the
intent here and ``discord_trailing_stop`` enforces it against live prices. Where
a native trailing stop IS available the broker holds it instead.

Emulation has a real consequence worth stating: the stop only fires while the
poller is running. A native stop rests at the broker and survives our downtime;
this one does not.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import or_ as sa_or, select
from sqlalchemy.orm import Session

from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

log = logging.getLogger(__name__)

# What a sell alert should do, decided by the guard.
# How the quantity being sold leaves.
MARKET = "market"           # sell it now
TRAIL = "trail"             # let it ride, exit on a dollar give-back
NONE = "none"               # nothing is being sold on this rung
OPEN = "open"               # a buy


@dataclass
class TrimConfig:
    """The trader's ladder settings, resolved by the caller."""

    profit_gate_pct: Decimal = Decimal("20")    # 1st trim only runs above this
    stop_pct: Decimal = Decimal("25")           # 1st trim's stop, below entry
    price_threshold: Decimal = Decimal("0.90")  # above this, exits trail
    trail_amount: Decimal = Decimal("0.25")     # dollar give-back that triggers


@dataclass
class TrimPlan:
    """What this exit alert should do. ``sell_qty`` of 0 means nothing leaves."""

    rung: int
    guard: "DiscordPositionGuard"
    sell_qty: Decimal = Decimal(0)
    exit_style: str = NONE
    trail_amount: Decimal | None = None
    new_stop_price: Decimal | None = None
    retire: bool = False
    note: str = ""


def _half(held: Decimal) -> Decimal:
    """Half a position, rounded UP to a whole contract.

    Rounding up rather than down keeps a trim from being a no-op: half of one
    contract rounds to zero, and an alert that sells nothing while still
    consuming a rung would walk the trader down the ladder without ever
    reducing the position. The caller treats "sold everything" as a close.
    """
    return (held / Decimal(2)).to_integral_value(rounding=ROUND_CEILING)


def plan_exit(
    guard: DiscordPositionGuard,
    held: Decimal,
    mark: Decimal | None,
    cfg: TrimConfig,
) -> TrimPlan:
    """Work out this alert's rung and what it does. Mutates ``guard``.

    The rung always advances, even when the trim itself does nothing. An alert
    that arrives below the profit gate is still the trader's first exit signal,
    so the next one has to read as the second — otherwise a quiet position could
    take an unlimited number of "first" alerts and never progress.
    """
    rung = (guard.sell_count or 0) + 1
    guard.sell_count = rung
    entry = guard.entry_price

    if held <= 0:
        return TrimPlan(rung=rung, guard=guard, retire=True,
                        note="nothing held")

    # ── rung 3 and beyond: everything goes ──────────────────────────────────
    if rung >= 3:
        style, amount = _exit_style(entry, cfg)
        return TrimPlan(
            rung=rung, guard=guard, sell_qty=held, exit_style=style,
            trail_amount=amount, retire=(style == MARKET),
            note=f"final exit of {held}",
        )

    # ── rung 1: gated on profit, and only ever sells at market ──────────────
    if rung == 1:
        if entry is None or entry <= 0 or mark is None or mark <= 0:
            return TrimPlan(
                rung=rung, guard=guard,
                note="no entry price or live mark — cannot measure profit",
            )
        gain_pct = (mark - entry) / entry * Decimal(100)
        if gain_pct <= cfg.profit_gate_pct:
            return TrimPlan(
                rung=rung, guard=guard,
                note=(f"up {gain_pct.quantize(Decimal('0.01'))}%, "
                      f"under the {cfg.profit_gate_pct}% gate — nothing sold"),
            )
        sell = _half(held)
        stop = entry * (Decimal(1) - cfg.stop_pct / Decimal(100))
        return TrimPlan(
            rung=rung, guard=guard, sell_qty=sell, exit_style=MARKET,
            new_stop_price=(None if sell >= held else _armable_stop(stop, mark)),
            retire=(sell >= held),
            note=(f"up {gain_pct.quantize(Decimal('0.01'))}% — sold {sell} of {held}"
                  + ("" if sell >= held else f", stop {stop.quantize(Decimal('0.0001'))}")),
        )

    # ── rung 2: half of what's left, remainder held at break-even ───────────
    sell = _half(held)
    style, amount = _exit_style(entry, cfg)
    return TrimPlan(
        rung=rung, guard=guard, sell_qty=sell, exit_style=style,
        trail_amount=amount,
        # Break-even on whatever is still held — but only if the position is
        # actually above it. Nothing left to protect if this rung takes it all.
        new_stop_price=(None if sell >= held else _armable_stop(entry, mark)),
        retire=(sell >= held and style == MARKET),
        note=f"sold {sell} of {held}, stop to break-even",
    )


def _armable_stop(stop: Decimal | None, mark: Decimal | None) -> Decimal | None:
    """A stop is only a stop if the price is still above it.

    Setting one at or below the current mark doesn't protect anything — the
    enforcer reads it as already breached and flattens the position on its next
    tick. That turns "move the stop to break-even" into "sell everything now"
    whenever the position happens to be underwater, which is exactly when the
    trader least wants to be forced out.

    Returning None leaves whatever stop was already there, so a trim can tighten
    protection but never trigger an exit by itself.
    """
    if stop is None or mark is None:
        return stop
    return stop if stop < mark else None


def _exit_style(entry: Decimal | None, cfg: TrimConfig) -> tuple[str, Decimal | None]:
    """Trail an expensive contract out; take a cheap one to market.

    A contract worth less than the threshold has little room left to give back —
    trailing it risks watching the remaining value evaporate for the sake of a
    move it can no longer make. Above the threshold there's enough left to be
    worth riding.
    """
    if entry is not None and entry > cfg.price_threshold:
        return TRAIL, cfg.trail_amount
    return MARKET, None


def arm_trail(guard: DiscordPositionGuard, qty: Decimal, amount: Decimal,
              mark: Decimal | None) -> None:
    """Park ``qty`` on a trailing exit instead of selling it now."""
    guard.trail_qty = qty
    guard.trail_amount = amount
    guard.peak_price = mark
    guard.armed_at = datetime.now(timezone.utc)


def clear_trail(guard: DiscordPositionGuard) -> None:
    guard.trail_qty = None
    guard.trail_amount = None
    guard.peak_price = None


def _match(q, user_id, symbol, strike, right, expiry):
    return q.where(
        DiscordPositionGuard.user_id == user_id,
        DiscordPositionGuard.symbol == symbol.upper(),
        DiscordPositionGuard.option_strike == strike,
        DiscordPositionGuard.option_right == (right.value if right else None),
        DiscordPositionGuard.option_expiry == expiry,
        DiscordPositionGuard.closed_at.is_(None),
    )


def find(db: Session, user_id: uuid.UUID, symbol: str, strike, right, expiry):
    """The live guard for this contract, if any."""
    return db.execute(
        _match(select(DiscordPositionGuard), user_id, symbol, strike, right, expiry)
    ).scalars().first()


def on_buy(
    db: Session, user_id: uuid.UUID, symbol: str,
    strike: Decimal | None, right: OptionRight | None, expiry: date | None,
    entry_price: Decimal | None = None,
) -> DiscordPositionGuard:
    """Record that a position is open and remember what it cost.

    Adding to an existing position does NOT reset the rung or re-price the
    entry. An "Adding" alert increases size; it doesn't restart the ladder, and
    it must not move a stop that is already protecting the position. Re-pricing
    on every add would let a position that kept averaging up quietly raise its
    own stop-loss under a trader who never asked for that.
    """
    guard = find(db, user_id, symbol, strike, right, expiry)
    if guard is not None:
        if guard.entry_price is None and entry_price is not None:
            # First price we've managed to learn for a position we were already
            # tracking — better than never having a reference at all.
            guard.entry_price = entry_price
        return guard

    guard = DiscordPositionGuard(
        user_id=user_id,
        symbol=symbol.upper(),
        option_strike=strike,
        option_right=(right.value if right else None),
        option_expiry=expiry,
        sell_count=0,
        entry_price=entry_price,
    )
    db.add(guard)
    db.flush()
    log.info("discord guard: opened for %s %s %s %s at %s",
             symbol, strike, right, expiry, entry_price)
    return guard


def rollback_exit(guard: DiscordPositionGuard) -> None:
    """Undo the rung bump from an exit whose order never made it.

    Nothing was sold, so consuming the rung would walk the trader down the
    ladder for a trim the broker refused — their next alert would jump to the
    step after the one that failed. Any stop this rung set is dropped too, since
    it was priced for a position size that never happened.
    """
    guard.sell_count = max(0, (guard.sell_count or 0) - 1)
    clear_trail(guard)


def retire(db: Session, guard: DiscordPositionGuard, reason: str) -> None:
    """Retire a guard once its position is gone. Kept rather than deleted so the
    history survives and a new position can reuse the same contract."""
    guard.closed_at = datetime.now(timezone.utc)
    guard.closed_reason = reason[:120]


def armed(db: Session) -> list[DiscordPositionGuard]:
    """Every live guard with something to enforce — a stop level, a trailing
    exit, or both."""
    return list(
        db.execute(
            select(DiscordPositionGuard).where(
                DiscordPositionGuard.closed_at.is_(None),
                DiscordPositionGuard.stop_order_id.is_(None),   # native stops are the broker's job
                sa_or(
                    DiscordPositionGuard.stop_price.is_not(None),
                    DiscordPositionGuard.trail_qty.is_not(None),
                ),
            )
        ).scalars()
    )


__all__ = [
    "MARKET", "NONE", "OPEN", "TRAIL", "TrimConfig", "TrimPlan",
    "arm_trail", "armed", "clear_trail", "find", "on_buy", "plan_exit",
    "retire", "rollback_exit",
]
