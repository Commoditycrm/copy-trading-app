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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

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


@dataclass(frozen=True)
class RungConfig:
    """One rung's two knobs, set independently of the other rungs.

    ``profit_gate_pct`` is the minimum gain over ENTRY before this trim sells
    anything; 0 means it always sells.

    ``stop_pct`` is SIGNED — the return the stop sits at, relative to entry.
    -25 is 25% below entry, 0 is break-even, +10 is 10% ABOVE entry and locks
    in profit. It reads as a return so that a stop and a profit target are
    written the same way; the unsigned version could never express a stop above
    break-even at all.

    ``qty_pct`` is how much of what is STILL HELD this rung sells. Of the
    remainder, not of the original position — that is what makes the rungs
    compose: 50 / 50 / 100 works down a position of 4 as 2, then 1, then 1,
    and it is the behaviour the ladder had before these were configurable.
    """

    profit_gate_pct: Decimal = Decimal("0")
    stop_pct: Decimal = Decimal("0")
    qty_pct: Decimal = Decimal("50")
    # The stop TRAILS: ``stop_pct`` is then a give-back from the high since this
    # trim (15 = 15% below the best price), not a return from entry.
    stop_trail: bool = False


@dataclass
class TrimConfig:
    """The trader's ladder settings, resolved by the caller.

    Each rung carries its OWN gate and stop, so changing the 1st trim cannot
    move the 2nd or 3rd. The defaults are the behaviour the ladder had before
    they were configurable: the 1st trim gated at +20% with a stop at -25%
    (25% below entry), and the 2nd and 3rd ungated with the remainder held at
    break-even (a stop of 0 IS break-even, which is why 0 is the default rather
    than a special case).
    """

    trim1: RungConfig = RungConfig(Decimal("20"), Decimal("-25"), Decimal("50"))
    trim2: RungConfig = RungConfig(Decimal("0"), Decimal("0"), Decimal("50"))
    trim3: RungConfig = RungConfig(Decimal("0"), Decimal("0"), Decimal("100"))
    price_threshold: Decimal = Decimal("0.90")  # above this, exits trail
    trail_amount: Decimal = Decimal("0.25")     # dollar give-back that triggers
    # The whole ladder, when it is not the classic three: any number of trims,
    # in order (services/discord_ladder builds it from the settings). When set
    # it is the ladder; trim1..trim3 above are then not consulted.
    rungs: tuple[RungConfig, ...] | None = None
    # Trims 2+ on an expensive contract ride a dollar give-back instead of going
    # to market. Off for the configured ladder (services/discord_ladder), which
    # trails the STOP per trim instead.
    trail_exits: bool = True

    def ladder(self) -> tuple[RungConfig, ...]:
        return self.rungs if self.rungs else (self.trim1, self.trim2, self.trim3)

    @property
    def count(self) -> int:
        """How many trims the ladder has."""
        return len(self.ladder())

    def rung(self, n: int) -> RungConfig:
        """This rung's settings. Rungs past the last reuse the last one's — an
        exit alert that arrives after the ladder is spent repeats its final step."""
        seq = self.ladder()
        return seq[n - 1] if 1 <= n <= len(seq) else seq[-1]


@dataclass
class TrimPlan:
    """What this exit alert should do. ``sell_qty`` of 0 means nothing leaves."""

    rung: int
    guard: "DiscordPositionGuard"
    sell_qty: Decimal = Decimal(0)
    exit_style: str = NONE
    trail_amount: Decimal | None = None
    new_stop_price: Decimal | None = None
    # Set with new_stop_price when that stop trails (see apply_stop).
    stop_trail_pct: Decimal | None = None
    stop_peak: Decimal | None = None
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


def _slice(held: Decimal, pct: Decimal | None) -> Decimal:
    """``pct`` percent of what is still held, rounded UP to a whole contract.

    Rounding up rather than down keeps a trim from being a no-op: 30% of one
    contract rounds to zero, and an alert that sells nothing while still
    consuming a rung would walk the trader down the ladder without ever
    reducing the position. Capped at the holding, so 100% (or anything above
    it) is a full exit rather than an oversell the broker would reject.
    """
    if held <= 0:
        return Decimal(0)
    pct = Decimal(str(pct if pct is not None else 50))
    if pct <= 0:
        return Decimal(0)
    want = (held * pct / Decimal(100)).to_integral_value(rounding=ROUND_CEILING)
    return want if want < held else held


# In a plan's note when the last trim rounded down to nothing: the rung is spent
# on purpose (auto-trim must not hand it back and fire it again every sweep).
RUNNER_NOTE = "runner left"


def _slice_down(held: Decimal, pct: Decimal | None) -> Decimal:
    """``pct`` percent of what is still held, rounded DOWN to a whole contract.

    The LAST trim's rule when it is not 100%: whatever the percentage does not
    cleanly take stays on as a runner. Every earlier trim rounds UP (see _slice)
    so it can never be a no-op; the last one is where the trader has said they
    want something left, so it never takes more than asked — 50% of 3 sells 1
    and leaves 2, and 50% of 1 sells nothing and leaves the 1.
    """
    if held <= 0:
        return Decimal(0)
    pct = Decimal(str(pct if pct is not None else 50))
    if pct <= 0:
        return Decimal(0)
    return (held * pct / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR)


def rung_quantity(cfg: "TrimConfig", rung: int, held: Decimal) -> tuple[Decimal, bool]:
    """How many contracts this trim sells out of ``held``, and whether it is a
    last trim that leaves a runner. One rule for a trim fired by an alert, by
    auto-trim, and for a take-profit order resting at the broker — the three
    must agree on the size or the ladder walks differently depending on how a
    trim happened to fire."""
    rung_cfg = cfg.rung(rung)
    leaves_runner = rung >= cfg.count and Decimal(str(rung_cfg.qty_pct)) < Decimal(100)
    sell = _slice_down(held, rung_cfg.qty_pct) if leaves_runner else _slice(held, rung_cfg.qty_pct)
    return sell, leaves_runner


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

    rung_cfg = cfg.rung(rung)

    gain_pct = None
    if entry is not None and entry > 0 and mark is not None and mark > 0:
        gain_pct = (mark - entry) / entry * Decimal(100)

    # This rung's stop, on whatever is still held after it. 0% below entry IS
    # break-even, which is what the 2nd trim has always done — so break-even
    # needs no special case.
    # A SIGNED offset from entry, read as the return the stop sits at:
    #
    #   -25  ->  entry x 0.75   25% below entry, the usual protective stop
    #     0  ->  entry          break-even
    #   +10  ->  entry x 1.10   10% ABOVE entry, locking in profit
    #
    # The sign is the whole point: without it the highest a stop could go was
    # break-even, so there was no way to say "the 2nd trim moves the stop to
    # +10%". Existing values were negated by migration e4c9d2a6b183, so a
    # ladder that read 25 (25% below) now reads -25 and sits where it always did.
    # A trailing stop instead starts that give-back below the price now.
    stop, trail_pct, peak = rung_stop(entry, rung_cfg, mark)
    trailing = {"stop_trail_pct": trail_pct, "stop_peak": peak}

    # A gate of 0 means NO minimum, not "must be at break-even or better".
    # That distinction is the difference between reproducing the old ladder and
    # quietly changing it: the 2nd and 3rd trims never had a gate, so an
    # UNDERWATER position still sold. Reading 0 as a threshold would make
    # `gain_pct < 0` refuse exactly the exits a losing position most needs. A
    # trader who wants "only in profit" sets a small positive number.
    gate = rung_cfg.profit_gate_pct or Decimal(0)
    if gate > 0:
        if gain_pct is None:
            # Only a GATED rung has to measure profit. An ungated one sells
            # whether or not a live mark happens to be available, which is how
            # rungs 2 and 3 behaved before they were configurable.
            return TrimPlan(
                rung=rung, guard=guard,
                note="no entry price or live mark — cannot measure profit",
            )
        # The gate is inclusive — "up 20%" trims AT a 20% gate, not past it.
        if gain_pct < gate:
            # The gate decides whether to SELL, not whether to protect. The
            # position is open either way, so it gets its stop either way —
            # otherwise an alert that arrives early leaves the trader holding an
            # unprotected position until the next one happens to come.
            return TrimPlan(
                rung=rung, guard=guard,
                new_stop_price=_armable_stop(stop, mark), **trailing,
                note=(f"trim {rung}: up {gain_pct.quantize(Decimal('0.01'))}%, "
                      f"under the {gate}% gate — nothing sold"
                      + (f", stop set at {stop.quantize(Decimal('0.0001'))}"
                         if stop is not None else "")),
            )

    # How much leaves: this rung's configured share of what is still held.
    # The defaults (50 / 50 / 100) reproduce the ladder exactly as it behaved
    # before the size was configurable.
    #
    # The LAST trim, when it is not 100%, rounds DOWN and leaves the balance as
    # a runner, still protected by this trim's stop.
    sell, leaves_runner = rung_quantity(cfg, rung, held)
    if leaves_runner and sell <= 0:
        return TrimPlan(
            rung=rung, guard=guard,
            new_stop_price=_armable_stop(stop, mark), **trailing,
            note=(f"trim {rung}: last trim at {rung_cfg.qty_pct.normalize():f}% of {held} "
                  f"rounds down to 0 — {RUNNER_NOTE}"
                  + (f", stop {stop.quantize(Decimal('0.0001'))}" if stop is not None else "")),
        )

    # The 1st trim always goes to market. Later rungs ride an expensive contract
    # out on a trailing give-back instead — a cheap one isn't worth trailing.
    style, amount = _exit_style(entry, cfg) if rung >= 2 and cfg.trail_exits else (MARKET, None)

    takes_everything = sell >= held
    gain_note = (
        f"up {gain_pct.quantize(Decimal('0.01'))}% — " if gain_pct is not None else ""
    )
    stop_note = (
        "" if takes_everything or stop is None
        else f", stop {stop.quantize(Decimal('0.0001'))}"
        + (f" trailing {trail_pct.normalize():f}%" if trail_pct is not None else "")
    )
    return TrimPlan(
        rung=rung, guard=guard, sell_qty=sell, exit_style=style,
        trail_amount=amount,
        # Nothing left to protect if this rung takes the whole position.
        new_stop_price=(None if takes_everything else _armable_stop(stop, mark)),
        **({} if takes_everything else trailing),
        retire=(takes_everything and style == MARKET),
        note=(f"trim {rung}: {gain_note}sold {sell} of {held}{stop_note}"
              + (f" — {held - sell} {RUNNER_NOTE}" if leaves_runner and sell < held else "")),
    )


# Brokers quote options in cents. entry x 0.75 routinely lands on a fraction of
# one (2.70 -> 2.0250), which Alpaca rejects outright: "stop price must be
# limited to 2 decimal places". Round DOWN so the rounding never tightens a stop
# the trader didn't ask to tighten.
_TICK = Decimal("0.01")


def _to_tick(price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    from decimal import ROUND_DOWN  # noqa: PLC0415

    return price.quantize(_TICK, rounding=ROUND_DOWN)


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
    stop = _to_tick(stop)
    # A deep stop on a cheap contract rounds down to $0.00 (entry 0.02 at -90%
    # is 0.002). That is not a stop, and the broker refuses it — which the stop
    # reconciler used to answer by selling the whole position. Live 2026-09-29.
    if stop is not None and stop <= 0:
        return None
    if stop is None or mark is None:
        return stop
    return stop if stop < mark else None


def rung_stop(entry, rung_cfg: RungConfig, mark) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Where a trim's stop goes: (stop price, trail %, peak).

    A FIXED stop is a return from entry (-25 -> entry x 0.75). A TRAILING one is
    a give-back from the best price since the trim — it starts that far below
    the price now (the entry when there is no mark yet) and ratchet_stop() raises
    it from there. Trail % and peak are None for a fixed stop."""
    entry = Decimal(str(entry)) if entry is not None else None
    if not rung_cfg.stop_trail:
        if entry is None or entry <= 0:
            return None, None, None
        return entry * (Decimal(1) + rung_cfg.stop_pct / Decimal(100)), None, None
    return trailing_stop(abs(Decimal(str(rung_cfg.stop_pct))), mark if mark else entry)


def trailing_stop(pct: Decimal, peak) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """A trailing stop ``pct`` % below ``peak``: (stop price, pct, peak)."""
    if peak is None or pct <= 0:
        return None, None, None
    peak = Decimal(str(peak))
    if peak <= 0:
        return None, None, None
    return peak * (Decimal(1) - pct / Decimal(100)), pct, peak


def apply_stop(guard: DiscordPositionGuard, plan: "TrimPlan") -> None:
    """Put a plan's stop on the guard — fixed, or trailing with its peak. A plan
    with no new stop leaves the one already there (fixed or trailing) alone."""
    if plan.new_stop_price is None:
        return
    guard.stop_price = plan.new_stop_price
    guard.stop_trail_pct = plan.stop_trail_pct
    guard.stop_peak = plan.stop_peak


# How far a trailing stop must be able to rise before it is moved. Each move
# replaces the order resting at the broker, so following every cent would spend
# the rate limit on churn; 2% (at least 2 cents) keeps it within a whisker of
# the true trail.
_RATCHET_PCT = Decimal("2")
_RATCHET_MIN = Decimal("0.02")


def ratchet_stop(guard: DiscordPositionGuard, price) -> bool:
    """Raise a trailing stop after a new high. Only ever up — a pullback leaves
    it where it is, which is what makes it a stop. Returns True when it moved."""
    pct = getattr(guard, "stop_trail_pct", None)
    if pct is None or price is None or guard.stop_price is None:
        return False
    price = Decimal(str(price))
    if price <= 0:
        return False
    peak = getattr(guard, "stop_peak", None)
    peak = Decimal(str(peak)) if peak is not None else None
    if peak is None or price > peak:
        guard.stop_peak = peak = price
    want = _to_tick(peak * (Decimal(1) - abs(Decimal(str(pct))) / Decimal(100)))
    current = Decimal(str(guard.stop_price))
    step = max(_RATCHET_MIN, current * _RATCHET_PCT / Decimal(100))
    if want is None or want <= 0 or want < current + step:
        return False
    guard.stop_price = want
    return True


def clear_stop_trail(guard: DiscordPositionGuard) -> None:
    """The stop is no longer the ladder's trailing one (set or removed by hand)."""
    guard.stop_trail_pct = None
    guard.stop_peak = None


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


def assigned(db: Session, user_id: uuid.UUID, symbol: str | None = None) -> list[DiscordPositionGuard]:
    """Live guards the trader assigned to a channel by hand (``source_id`` set),
    newest first — optionally only one symbol's."""
    q = select(DiscordPositionGuard).where(
        DiscordPositionGuard.user_id == user_id,
        DiscordPositionGuard.closed_at.is_(None),
        DiscordPositionGuard.source_id.isnot(None),
    )
    if symbol:
        q = q.where(DiscordPositionGuard.symbol == symbol.upper())
    return list(db.execute(q.order_by(DiscordPositionGuard.created_at.desc())).scalars())


def contract_key(strike, right, expiry) -> tuple:
    """(strike, right, expiry) with the right as its plain value — a guard
    stores "call"/"put", a broker position carries the enum."""
    return (strike, getattr(right, "value", right) or None, expiry)


def _holding_average(db: Session, guard: DiscordPositionGuard) -> Decimal | None:
    """The average cost of this holding: every BUY of the contract that filled
    since the guard opened (the entry and each average), weighted by quantity.
    None when nothing has filled, or the orders can't be read."""
    from app.models.order import Order, OrderSide  # noqa: PLC0415

    try:
        q = select(Order).where(
            Order.user_id == guard.user_id,
            Order.symbol == (guard.symbol or "").upper(),
            Order.side == OrderSide.BUY,
            Order.is_closing.is_(False),
            Order.filled_quantity > 0,
            Order.filled_avg_price.isnot(None),
        )
        right = getattr(guard.option_right, "value", guard.option_right)
        for col, val in ((Order.option_strike, guard.option_strike),
                         (Order.option_right, OptionRight(right) if right else None),
                         (Order.option_expiry, guard.option_expiry)):
            q = q.where(col.is_(None) if val is None else col == val)
        opened = getattr(guard, "created_at", None)
        if opened is not None:
            q = q.where(Order.created_at >= opened)
        orders = list(db.execute(q).scalars())
    except Exception:  # noqa: BLE001 — a database that can't answer: no average
        return None
    qty = sum((Decimal(str(o.filled_quantity)) for o in orders), Decimal(0))
    if qty <= 0:
        return None
    cost = sum((Decimal(str(o.filled_quantity)) * Decimal(str(o.filled_avg_price)) for o in orders), Decimal(0))
    return (cost / qty).quantize(Decimal("0.0001"))


def sync_entry_price(db: Session, guard: DiscordPositionGuard, ts=None) -> bool:
    """Adopt what the position ACTUALLY cost. Returns True if it moved.

    ``entry_price`` is seeded at placement with the limit we bid, because that
    is the only reference that exists before the order fills, and an average
    re-weights it at placement the same way (average_in). Both are corrected
    here from the fills: the average cost of every buy of this holding — the
    entry and each average — weighted by quantity.

    (It used to adopt only the OPENING order's fill. That undid every average:
    averaging 4 @ 0.41 with 4 @ 0.38 re-weighted the entry to 0.395, and the
    next pass put it back to 0.41 — targets and stops kept measuring from a
    price no longer paid.)

    When it moves and ``ts`` (the settings that govern the position) is given,
    the ladder's stop moves with it — see reprice_ladder_stop.
    """
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    filled = _holding_average(db, guard)
    if filled is None:
        # Fall back to the opening order's own fill.
        if guard.entry_order_id is None:
            return False
        order = db.get(Order, guard.entry_order_id)
        if order is None or order.status not in (
            OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
        ):
            return False
        if order.filled_avg_price is None or Decimal(str(order.filled_avg_price)) <= 0:
            return False
        filled = Decimal(str(order.filled_avg_price))
    if filled <= 0:
        return False
    if guard.entry_price is not None and Decimal(str(guard.entry_price)) == filled:
        return False
    old = guard.entry_price
    log.info("discord guard: %s entry %s -> %s (actual cost)", guard.symbol, old, filled)
    guard.entry_price = filled
    if ts is not None:
        reprice_ladder_stop(guard, old, filled, ts)
    return True


def reprice_ladder_stop(guard: DiscordPositionGuard, old_entry, new_entry, ts) -> bool:
    """After the entry moved (an average filled), move the LADDER's stop with it.

    The stop the ladder set is a return from entry — the On Fill stop before any
    trim, else the stop of the last trim — so it is recomputed from the new
    average. Only when the stop sits exactly where the ladder put it: a stop set
    by hand is the trader's, and a trailing stop measures from its high, not
    from entry, so neither is touched. Returns True when it moved.
    """
    from app.services import discord_ladder  # noqa: PLC0415

    if (old_entry is None or new_entry is None or guard.stop_price is None
            or getattr(guard, "stop_trail_pct", None) is not None):
        return False
    old_entry, new_entry = Decimal(str(old_entry)), Decimal(str(new_entry))
    rung = guard.sell_count or 0
    if rung == 0:
        if discord_ladder.fill_stop_trails(ts):
            return False
        pct = discord_ladder.fill_stop_pct(ts)
    else:
        cfg = discord_ladder.trim_config(ts).rung(rung)
        if cfg.stop_trail:
            return False
        pct = cfg.stop_pct
    if pct is None:
        return False
    pct = Decimal(str(pct))
    was = _to_tick(old_entry * (Decimal(1) + pct / Decimal(100)))
    if was is None or Decimal(str(guard.stop_price)) != was:
        return False                      # not the ladder's level: set by hand
    now = _to_tick(new_entry * (Decimal(1) + pct / Decimal(100)))
    if now is None or now <= 0 or now == was:
        return False
    from app.services.position_events import because  # noqa: PLC0415

    with because(f"averaged — the ladder's stop recalculated from the new average {_to_tick(new_entry)}"):
        guard.stop_price = now
    log.info("discord guard: %s averaged %s -> %s; ladder stop %s -> %s",
             guard.symbol, old_entry, new_entry, was, now)
    return True


def retire_if_flat(db: Session, guard: DiscordPositionGuard, held: Decimal) -> bool:
    """Retire a guard whose position is gone. Returns True if it retired.

    A guard outlives its position whenever the position leaves by a route the
    ladder did not drive -- a manual close from the positions table, a stop that
    filled at the broker, an expiry. It then keeps its rung and its stop LEVEL,
    and the next BUY on that contract inherits both: live, a fresh NIO entry at
    0.22 was immediately covered by a SELL 4 STOP @ 0.15 carried over from the
    previous position, off a rung that had already reached 2.

    Only retires a guard that was actually protecting something. An entry that
    has not filled yet ALSO reports held == 0, and retiring that would drop the
    ladder before the position even opens.
    """
    if held > 0:
        return False
    if ((guard.sell_count or 0) <= 0
            and guard.stop_price is None and guard.trail_qty is None):
        return False
    retire(db, guard, f"position flat (rung {guard.sell_count or 0})")
    return True


def dormant(db: Session, guard: DiscordPositionGuard) -> bool:
    """True when this guard is not protecting anything yet.

    Nothing sold, no stop or trail resting, and no opening order that actually
    filled. In that state the guard describes an INTENT to hold, not a holding,
    so re-pricing it costs nothing — whereas inheriting its price silently
    mis-measures the whole ladder for the next position on that contract.
    """
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    if (guard.sell_count or 0) > 0:
        return False
    if guard.stop_order_id is not None or guard.trail_qty is not None:
        return False
    if guard.entry_order_id is None:
        # No link, so we cannot show the position never opened — and "I cannot
        # tell" must not license a re-price. Guards created before that column
        # keep the old behaviour and age out on their own.
        return False
    order = db.get(Order, guard.entry_order_id)
    if order is None:
        return False
    return order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)


def on_buy(
    db: Session, user_id: uuid.UUID, symbol: str,
    strike: Decimal | None, right: OptionRight | None, expiry: date | None,
    entry_price: Decimal | None = None,
    entry_order_id: uuid.UUID | None = None,
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
        elif dormant(db, guard):
            # This guard describes a position that never opened: its entry was
            # cancelled or is still working, nothing has been sold off it, and
            # no stop is resting. A guard is created when the BUY is placed, not
            # when it fills, so an entry that never fills leaves one behind —
            # and the next real position on that contract inherits its price.
            #
            # That happened live: four NIO entries were placed and cancelled at
            # 0.16/0.11, then a fifth filled at 0.24. The stale guard still read
            # 0.15, so the first trim's stop went to 0.11 (-25% of a price never
            # paid) instead of 0.18. Re-seed rather than inherit.
            #
            # Deliberately NOT a general re-price: a guard with a filled entry,
            # an advanced rung or a resting stop is protecting something real,
            # and an "Adding" alert must never move that.
            log.info(
                "discord guard: re-seeding dormant %s guard %s -> %s "
                "(previous entry never filled)",
                symbol, guard.entry_price, entry_price,
            )
            guard.entry_price = entry_price
            guard.entry_order_id = entry_order_id
            guard.fill_stop_done = False      # a new holding: its On Fill stop is still to come
        elif guard.entry_order_id is None and entry_order_id is not None:
            guard.entry_order_id = entry_order_id
        return guard

    guard = DiscordPositionGuard(
        user_id=user_id,
        symbol=symbol.upper(),
        option_strike=strike,
        option_right=(right.value if right else None),
        option_expiry=expiry,
        sell_count=0,
        entry_price=entry_price,
        entry_order_id=entry_order_id,
    )
    db.add(guard)
    db.flush()
    log.info("discord guard: opened for %s %s %s %s at %s",
             symbol, strike, right, expiry, entry_price)
    return guard


def average_in(
    db: Session, guard: DiscordPositionGuard, *,
    held_qty: Decimal, added_qty: Decimal, added_price: Decimal | None,
) -> Decimal | None:
    """Re-weight ``entry_price`` after AVERAGING DOWN. Returns the new average.

    Normally an add must NOT move the reference — see on_buy. A position that
    kept averaging UP would otherwise quietly raise its own stop-loss under a
    trader who never asked for that, so the ladder holds the first fill fixed.

    Averaging down is the case where holding it fixed is the error, and by a
    wide margin. Doubling 4 @ 0.68 into 8 @ 0.40 makes the real cost 0.54, and
    doubling again at 0.23 makes it 0.385 — while the guard still reads 0.68.
    Every level then means something the trader never chose:

        stop (-25%)        0.5100   vs   0.2888 off the real average
        trim gate (+20%)   0.8160   vs   0.4620

    A stop at 0.51 sits ABOVE the true cost of 0.385, so it exits a position
    that is actually in profit; and a gate at 0.816 needs +112% over real cost,
    so no trim ever fires. Both are worse than the risk on_buy is guarding
    against, which is why this is a separate, explicit call rather than a
    loosening of on_buy.

    Weighted by quantity, so it stays right if an add is ever sized differently
    from the position:

        (held x entry + added x price) / (held + added)

    ``added_price`` is the LIMIT we bid, not the fill — the fill is not known at
    placement. A limit buy fills at or better than its limit, so the true
    average can only be LOWER than this, which errs toward a stop that exits
    early rather than one that sits under the real cost.
    """
    if added_price is None or added_price <= 0:
        return guard.entry_price
    if guard.entry_price is None:
        # Nothing to weight against — the add IS the only price we know.
        guard.entry_price = added_price
        return guard.entry_price
    held_qty = Decimal(str(held_qty or 0))
    added_qty = Decimal(str(added_qty or 0))
    total = held_qty + added_qty
    if total <= 0 or added_qty <= 0:
        return guard.entry_price

    previous = Decimal(str(guard.entry_price))
    new_avg = (
        (held_qty * previous + added_qty * Decimal(str(added_price))) / total
    ).quantize(Decimal("0.0001"))
    log.info(
        "discord guard: %s averaged down — entry %s -> %s (%s held @ %s + %s @ %s)",
        guard.symbol, previous, new_avg, held_qty, previous, added_qty, added_price,
    )
    guard.entry_price = new_avg
    return new_avg


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


def live(db: Session, user_id: uuid.UUID) -> list[DiscordPositionGuard]:
    """Every live guard of one trader, armed or not."""
    return list(
        db.execute(
            select(DiscordPositionGuard).where(
                DiscordPositionGuard.user_id == user_id,
                DiscordPositionGuard.closed_at.is_(None),
            )
        ).scalars()
    )


def armed(db: Session) -> list[DiscordPositionGuard]:
    """Every live guard with something to enforce — a stop level, a trailing
    exit, or both."""
    return list(
        db.execute(
            select(DiscordPositionGuard).where(
                DiscordPositionGuard.closed_at.is_(None),
                # Guards WITH a broker stop are included on purpose: the stop
                # still has to be reconciled each tick against the quantity
                # actually held, and cancelled when the position goes away.
                sa_or(
                    DiscordPositionGuard.stop_price.is_not(None),
                    DiscordPositionGuard.trail_qty.is_not(None),
                ),
            )
        ).scalars()
    )


__all__ = [
    "MARKET", "NONE", "OPEN", "TRAIL", "RungConfig", "TrimConfig", "TrimPlan",
    "arm_trail", "armed", "clear_trail", "rung_stop", "trailing_stop", "apply_stop",
    "ratchet_stop", "clear_stop_trail", "find", "on_buy", "plan_exit",
    "dormant", "retire", "retire_if_flat", "rollback_exit", "sync_entry_price",
]
