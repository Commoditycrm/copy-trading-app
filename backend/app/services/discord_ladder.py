"""The exit ladder as configured: an On Fill stop, then any number of trims.

    On Fill   stop                      — where the stop goes when the entry fills
    Trim 1    profit target, qty, stop
    Trim 2    profit target, qty, stop
    …         as many as the trader adds

Storage is split for history's sake: trims 1–3 are the original columns on
TraderSettings (a ladder nobody has edited reads exactly as it always did), and
trims past the third are JSON in ``discord_extra_trims``. ``discord_trim_count``
says how many are in use. Everything reads the ladder through here — the
settings API, the exit planner, auto-trim, the simulator — so that split never
leaks. ``ts`` is a TraderSettings row or a channel's own settings
(discord_channel_settings.ChannelSettings); both read the same way.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.services import discord_position_guard as guards

MAX_TRIMS = 10

# (profit target, stop, qty) for trims 1–3 when a value was never set: the
# ladder as it behaved before any of this was configurable.
_DEFAULTS = (("20", "-25", "50"), ("0", "0", "50"), ("0", "0", "100"))
_COLUMNS = (
    ("discord_trim_profit_gate_pct", "discord_trim_stop_pct", "discord_trim_qty_pct"),
    ("discord_trim2_profit_gate_pct", "discord_trim2_stop_pct", "discord_trim2_qty_pct"),
    ("discord_trim3_profit_gate_pct", "discord_trim3_stop_pct", "discord_trim3_qty_pct"),
)
# A trim added past the third starts as "sell the rest, stop at break-even".
NEW_TRIM = ("0", "0", "100")


def _dec(raw: Any, default: str) -> Decimal:
    if raw is None or raw == "":
        return Decimal(default)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def count(ts) -> int:
    """How many trims the ladder has — 3 unless the trader changed it."""
    try:
        n = int(getattr(ts, "discord_trim_count", None) or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(MAX_TRIMS, n))


def rungs(ts) -> list[guards.RungConfig]:
    """Every trim, in order."""
    out = [
        guards.RungConfig(
            _dec(getattr(ts, gate, None), d[0]),
            _dec(getattr(ts, stop, None), d[1]),
            _dec(getattr(ts, qty, None), d[2]),
        )
        for (gate, stop, qty), d in zip(_COLUMNS, _DEFAULTS)
    ]
    extra = getattr(ts, "discord_extra_trims", None)
    for item in (extra if isinstance(extra, list) else []):
        if not isinstance(item, dict):
            continue
        out.append(guards.RungConfig(
            _dec(item.get("profit_gate_pct"), NEW_TRIM[0]),
            _dec(item.get("stop_pct"), NEW_TRIM[1]),
            _dec(item.get("qty_pct"), NEW_TRIM[2]),
        ))
    n = count(ts)
    while len(out) < n:                       # count says more than is stored
        out.append(guards.RungConfig(*(Decimal(x) for x in NEW_TRIM)))
    return out[:n]


def fill_stop_pct(ts) -> Decimal | None:
    """The On Fill stop as a return from entry, or None when there is none."""
    raw = getattr(ts, "discord_fill_stop_pct", None)
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def trim_config(ts) -> guards.TrimConfig:
    """The ladder a live trim and the Simulated Prices dry run measure against."""
    return guards.TrimConfig(
        rungs=tuple(rungs(ts)),
        price_threshold=_dec(getattr(ts, "discord_trim_price_threshold", None), "0.90"),
        trail_amount=_dec(getattr(ts, "discord_trim_trail_amount", None), "0.25"),
    )


def store(ts, trims: list[tuple[Decimal, Decimal, Decimal]]) -> None:
    """Write the whole ladder: ``trims`` is (profit target, stop, qty) per trim,
    in order. The first three go to their columns, the rest to JSON."""
    for (gate_col, stop_col, qty_col), (gate, stop, qty) in zip(_COLUMNS, trims):
        setattr(ts, gate_col, gate)
        setattr(ts, stop_col, stop)
        setattr(ts, qty_col, qty)
    ts.discord_extra_trims = [
        {"profit_gate_pct": str(gate), "stop_pct": str(stop), "qty_pct": str(qty)}
        for gate, stop, qty in trims[3:]
    ]
    ts.discord_trim_count = len(trims)


__all__ = ["MAX_TRIMS", "count", "rungs", "fill_stop_pct", "trim_config", "store"]
