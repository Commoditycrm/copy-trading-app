"""Dry run of the exit ladder along a price path — no broker, no database.

Pinning a price drives the REAL pipeline: the pin reaches auto-trim and the
stop enforcer, and they place real orders. That is the right test while the
market is open, and no test at all while it is closed — nothing fills, so the
ladder never gets past its first order. This walks a scratch copy of the guard
down a list of prices instead, and says what happens at each step.

── The same code, not a model of it ─────────────────────────────────────────
Each step asks the three pieces the live path asks, in the order it asks them:

  * ``discord_trailing_stop.decide``  — does the stop or the trailing exit fire?
  * ``discord_auto_trim.due_rung``    — has the next trim's Min Profit been hit?
  * ``discord_position_guard.plan_exit`` — what that trim sells, where the stop goes

and applies the plan the way ``_execute_signal`` does: a TRAIL plan arms a
trailing exit instead of selling, a plan that sells nothing hands the rung back
(as auto-trim does), a plan that takes everything closes the position. A second
simulator with its own copy of these rules would drift from the real one, and
the drift is exactly what a dry run exists to catch.

── What it cannot know ──────────────────────────────────────────────────────
Fills are assumed in full at the step's price. A real market order fills at the
real price, a thin contract may not fill at all, and a real stop can be refused
by the broker (Alpaca rejects a stop at or above the market). The page says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

from app.services import discord_auto_trim as auto_trim
from app.services import discord_position_guard as guards
from app.services import discord_trailing_stop as protections

_CENT = Decimal("0.01")


@dataclass
class SimEvent:
    # hold | trim | trail_armed | stop_moved | stop_hit | trail_hit | flat | note
    kind: str
    text: str
    rung: int | None = None
    sold: Decimal = Decimal(0)


@dataclass
class SimStep:
    index: int
    pct: Decimal
    price: Decimal
    gain_pct: Decimal | None
    held: Decimal           # after this step
    stop: Decimal | None    # after this step
    events: list[SimEvent] = field(default_factory=list)


class _AutoTrimOn:
    """The trader's settings with Auto Trim forced on, for a trader who has it
    off: a dry run of the ladder is still worth seeing."""

    def __init__(self, ts):
        self._ts = ts

    def __getattr__(self, name):
        if name == "discord_auto_trim":
            return True
        if name == "discord_manual_exit":
            return False               # the dry run shows the ladder regardless
        return getattr(self._ts, name, None)


def _p(v: Decimal | None) -> str:
    if v is None:
        return "—"
    s = format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _pct(v: Decimal | None) -> str:
    return "—" if v is None else f"{v.quantize(_CENT):+}%"


def simulate(ts, cfg: guards.TrimConfig, entry: Decimal, qty: Decimal,
             pcts: list[Decimal]) -> list[SimStep]:
    """Walk a fresh position of ``qty`` bought at ``entry`` along ``pcts``
    (percent from entry, one per step). Pure: returns the narration."""
    guard = SimpleNamespace(
        sell_count=0, entry_price=entry, stop_price=None, stop_order_id=None,
        trail_qty=None, trail_amount=None, peak_price=None, armed_at=None,
        stop_trail_pct=None, stop_peak=None,
    )
    settings = ts if auto_trim._enabled(ts) else _AutoTrimOn(ts)
    held = qty
    steps: list[SimStep] = []

    for index, pct in enumerate(pcts, start=1):
        price = (entry * (Decimal(1) + pct / Decimal(100))).quantize(Decimal("0.0001"))
        gain = auto_trim.gain_pct(entry, price)
        events: list[SimEvent] = []
        steps.append(SimStep(index, pct, price, gain, held, guard.stop_price, events))

        if held <= 0:
            events.append(SimEvent("flat", "Position already closed — nothing left to manage."))
            continue

        # ── protections first: the poller checks them every tick ──────────
        decision = protections.decide(guard, price, held)
        if decision is not None:
            kind, sell, level = decision
            held -= sell
            if kind == protections.STOP:
                events.append(SimEvent(
                    "stop_hit",
                    f"STOP HIT — {_p(price)} is at or below the {_p(level)} stop. "
                    f"Sell {_p(sell)} at market (simulated fill {_p(price)}). Position closed.",
                    sold=sell,
                ))
                guard.stop_price = None
                guards.clear_trail(guard)
            else:
                left = "Position closed." if held <= 0 else f"{_p(held)} left."
                events.append(SimEvent(
                    "trail_hit",
                    f"TRAILING EXIT — gave back ${_p(guard.trail_amount)} from the "
                    f"{_p(level)} peak. Sell {_p(sell)} at market (simulated fill "
                    f"{_p(price)}). {left}",
                    sold=sell,
                ))
                guards.clear_trail(guard)
                if held <= 0:
                    guard.stop_price = None
            steps[-1].held, steps[-1].stop = held, guard.stop_price
            if held <= 0:
                continue

        # ── then auto-trim ─────────────────────────────────────────────────
        rung = auto_trim.due_rung(settings, guard, price)
        if rung is None:
            events.append(SimEvent("hold", _hold_text(settings, guard, gain)))
            steps[-1].held, steps[-1].stop = held, guard.stop_price
            continue

        before_stop = guard.stop_price
        plan = guards.plan_exit(guard, held, price, cfg)
        guards.apply_stop(guard, plan)
        stop_text = (
            f" Stop on the rest moved to {_p(guard.stop_price)}."
            if guard.stop_price is not None and guard.stop_price != before_stop else ""
        )

        if plan.sell_qty <= 0:
            # Auto-trim hands back a rung that sold nothing; any stop it set stays.
            guards.rollback_exit(guard)
            events.append(SimEvent("note", f"Trim {rung}: {plan.note} — rung returned.{stop_text}", rung))
        elif plan.exit_style == guards.TRAIL:
            guards.arm_trail(guard, plan.sell_qty, plan.trail_amount, price)
            events.append(SimEvent(
                "trail_armed",
                f"TRIM {rung} at {_pct(gain)} — contract above ${_p(cfg.price_threshold)}, so "
                f"{_p(plan.sell_qty)} of {_p(held)} rides a ${_p(plan.trail_amount)} trailing "
                f"exit instead of selling now.{stop_text}",
                rung,
            ))
        else:
            held -= plan.sell_qty
            closed = " Position closed." if held <= 0 else f" {_p(held)} left."
            events.append(SimEvent(
                "trim",
                f"TRIM {rung} at {_pct(gain)} — sell {_p(plan.sell_qty)} of "
                f"{_p(held + plan.sell_qty)} at market (simulated fill {_p(price)})."
                f"{closed}{stop_text if held > 0 else ''}",
                rung, plan.sell_qty,
            ))
            if held <= 0:
                guard.stop_price = None
                guards.clear_trail(guard)
        steps[-1].held, steps[-1].stop = held, guard.stop_price

    return steps


def _hold_text(ts, guard, gain: Decimal | None) -> str:
    """Why nothing happened, and what would happen next."""
    nxt = (guard.sell_count or 0) + 1
    parts = [f"Holding at {_pct(gain)}."]
    if nxt > 3:
        parts.append("All three trims are done.")
    else:
        gate = auto_trim._gate_for(ts, nxt)
        if gate <= 0:
            parts.append(f"Trim {nxt} has no Min Profit, so auto-trim never fires it — "
                         "live, it waits for a Discord alert.")
        else:
            parts.append(f"Trim {nxt} fires at +{_p(gate)}%.")
    if guard.stop_price is not None:
        parts.append(f"Stop {_p(guard.stop_price)}.")
    if guard.trail_qty is not None:
        parts.append(f"Trailing {_p(guard.trail_qty)} from peak {_p(guard.peak_price)} "
                     f"(exits at {_p(guard.peak_price - guard.trail_amount)}).")
    return " ".join(parts)
