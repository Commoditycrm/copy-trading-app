"""Enforce emulated trailing stops on Discord positions.

Armed by the FIRST sell alert on a position (see discord_position_guard). Each
tick: read the live price, raise the recorded peak, and close the position when
it has given back ``trail_percent`` from that peak.

── Why this is emulated ─────────────────────────────────────────────────────────
Alpaca rejects trailing-stop orders on options, and Discord alerts are almost
entirely options. A native stop would rest at the broker; this one only exists
while the poller runs, so a restart mid-session means the trail is only enforced
again from the next tick. The peak survives (it's on the row), so the stop
resumes where it left off rather than re-anchoring — which would silently widen
it.

Runs off pnl_poller, alongside position_enforcer, so it inherits the same 5s
cadence and per-user broker session rather than opening its own.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.discord_position_guard import DiscordPositionGuard
from app.services import discord_position_guard as guards

log = logging.getLogger(__name__)


def enforce(db: Session, user_id, adapter, close_position) -> int:
    """Advance every armed trail for this user. Returns how many were closed.

    ``close_position(position, guard)`` is injected so this module never places
    orders itself — the caller owns that, and can route it through the same path
    everything else uses.
    """
    rows = [g for g in guards.armed(db) if g.user_id == user_id]
    if not rows:
        return 0

    try:
        positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        # A failed read is not a reason to exit anything. Skip the tick.
        log.warning("discord trail: position read failed for user=%s", user_id, exc_info=True)
        return 0

    by_contract = {_key_of_position(p): p for p in positions}
    closed = 0

    for guard in rows:
        pos = by_contract.get(_key_of_guard(guard))
        if pos is None:
            # Position gone — closed by hand, expired, or stopped out elsewhere.
            guards.retire(db, guard, "position no longer held")
            continue

        price = _current_price(pos)
        if price is None or price <= 0:
            continue    # no usable mark this tick

        peak = guard.peak_price
        if peak is None or price > peak:
            guard.peak_price = price
            continue    # a new high can't also be a retrace

        trail = guard.trail_percent or Decimal(0)
        if trail <= 0:
            continue
        trigger = peak * (Decimal(1) - trail / Decimal(100))
        if price > trigger:
            continue

        log.info(
            "discord trail: %s retraced to %s from peak %s (%s%% trail) — closing",
            guard.symbol, price, peak, trail,
        )
        try:
            close_position(pos, guard)
        except Exception:  # noqa: BLE001
            # Leave the guard armed so the next tick tries again — an exit that
            # failed once must not be forgotten.
            log.exception("discord trail: close failed for %s", guard.symbol)
            continue
        guards.retire(db, guard, f"trailing stop hit ({trail}% from {peak})")
        closed += 1

    return closed


def _key_of_position(p) -> tuple:
    return (
        (p.symbol or "").upper(),
        p.option_strike,
        p.option_right.value if p.option_right else None,
        p.option_expiry,
    )


def _key_of_guard(g: DiscordPositionGuard) -> tuple:
    return (g.symbol.upper(), g.option_strike, g.option_right, g.option_expiry)


def _current_price(pos) -> Decimal | None:
    """Live price per contract/share.

    Prefers the broker's own current_price; falls back to deriving it from
    market value, which some adapters populate when current_price is absent.
    """
    if pos.current_price is not None:
        return Decimal(str(pos.current_price))
    qty = Decimal(str(pos.quantity or 0))
    if pos.market_value is not None and qty != 0:
        per_unit = Decimal(str(pos.market_value)) / abs(qty)
        # Options quote per share but are valued per contract (x100).
        if pos.option_strike is not None:
            per_unit = per_unit / Decimal(100)
        return per_unit
    return None


__all__ = ["enforce"]
