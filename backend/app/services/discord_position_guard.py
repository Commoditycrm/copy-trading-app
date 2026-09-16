"""What a SELL alert means depends on what came before it.

    BUY        → open the position, start counting
    1st SELL   → do NOT exit; arm a trailing stop to protect the gain
    2nd SELL   → close the position

So an exit alert is not self-contained: the same message is a "protect" or an
"exit" depending on the position's history. That history lives in
``DiscordPositionGuard``, one row per open contract.

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
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

log = logging.getLogger(__name__)

# What a sell alert should do, decided by the guard.
ARM_TRAIL = "arm_trail"     # first sell — protect, don't exit
CLOSE = "close"             # second sell — get out
OPEN = "open"               # a buy


@dataclass
class SellDecision:
    action: str
    guard: DiscordPositionGuard
    trail_percent: Decimal | None = None


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
) -> DiscordPositionGuard:
    """Record that a position is open and reset its sell count.

    Adding to an existing position does NOT reset the count — an "Adding" alert
    increases size, it doesn't start the trail-then-exit sequence over. Resetting
    would mean a second sell after an add merely re-arms the stop instead of
    closing, leaving the trader in a position they asked twice to leave.
    """
    guard = find(db, user_id, symbol, strike, right, expiry)
    if guard is not None:
        return guard

    guard = DiscordPositionGuard(
        user_id=user_id,
        symbol=symbol.upper(),
        option_strike=strike,
        option_right=(right.value if right else None),
        option_expiry=expiry,
        sell_count=0,
    )
    db.add(guard)
    db.flush()
    log.info("discord guard: opened for %s %s %s %s", symbol, strike, right, expiry)
    return guard


def on_sell(
    db: Session, user_id: uuid.UUID, symbol: str,
    strike: Decimal | None, right: OptionRight | None, expiry: date | None,
    trail_percent: Decimal,
) -> SellDecision:
    """Decide what this sell alert should do, and record it.

    No guard at all means we never saw the opening buy — a position opened
    elsewhere, or one from before this feature. Treat that as a CLOSE: the
    trader asked to exit, and refusing because we lack history would leave them
    holding something they tried to sell.
    """
    guard = find(db, user_id, symbol, strike, right, expiry)
    if guard is None:
        guard = DiscordPositionGuard(
            user_id=user_id,
            symbol=symbol.upper(),
            option_strike=strike,
            option_right=(right.value if right else None),
            option_expiry=expiry,
            # Counted as the second sell so this exits rather than arming a
            # trail on a position we know nothing about.
            sell_count=2,
        )
        db.add(guard)
        db.flush()
        log.info("discord guard: sell with no known entry for %s — closing", symbol)
        return SellDecision(action=CLOSE, guard=guard)

    guard.sell_count = (guard.sell_count or 0) + 1

    if guard.sell_count == 1:
        # Capture the trail NOW so a later settings change can't move the stop
        # on a position already being protected.
        guard.trail_percent = trail_percent
        guard.armed_at = datetime.now(timezone.utc)
        log.info(
            "discord guard: first sell for %s — arming %s%% trail",
            symbol, trail_percent,
        )
        return SellDecision(action=ARM_TRAIL, guard=guard, trail_percent=trail_percent)

    log.info("discord guard: sell #%s for %s — closing", guard.sell_count, symbol)
    return SellDecision(action=CLOSE, guard=guard)


def retire(db: Session, guard: DiscordPositionGuard, reason: str) -> None:
    """Retire a guard once its position is gone. Kept rather than deleted so the
    history survives and a new position can reuse the same contract."""
    guard.closed_at = datetime.now(timezone.utc)
    guard.closed_reason = reason[:120]


def armed(db: Session) -> list[DiscordPositionGuard]:
    """Every live guard with an emulated trail to enforce."""
    return list(
        db.execute(
            select(DiscordPositionGuard).where(
                DiscordPositionGuard.closed_at.is_(None),
                DiscordPositionGuard.armed_at.is_not(None),
                DiscordPositionGuard.stop_order_id.is_(None),   # native stops are the broker's job
            )
        ).scalars()
    )


__all__ = [
    "ARM_TRAIL", "CLOSE", "OPEN", "SellDecision",
    "armed", "find", "on_buy", "on_sell", "retire",
]
