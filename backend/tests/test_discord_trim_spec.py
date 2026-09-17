"""Conformance: the trim ladder as specified, walked end to end.

Every other trim test checks one rule in isolation. This one runs the whole
sequence the spec describes — buy, three alerts, the enforcer in between — and
asserts the position size and stop level after each step. If a rule is right on
its own but wrong in sequence, this is what catches it.

    BUY 4 @ 2.00
    alert 1 (up 50%)  → sell 2, stop 1.50   (25% below entry)
    alert 2           → sell 1, stop 2.00   (break-even)
    alert 3           → exit the rest
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
import app.services.discord_trailing_stop as stops
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight

ENTRY = Decimal("2.00")
CHEAP = Decimal("0.50")        # under the $0.90 threshold → exits at market


class _Pos:
    def __init__(self, qty, price):
        self.symbol = "MSFT"
        self.option_strike = Decimal("500")
        self.option_right = OptionRight.CALL
        self.option_expiry = None
        self.quantity = Decimal(qty)
        self.current_price = Decimal(price)
        self.instrument_type = InstrumentType.OPTION


class _Adapter:
    def __init__(self, pos): self._p = [pos]
    def get_positions(self): return self._p


class _Ladder:
    """Drives the ladder the way the API and poller do, tracking the position."""

    def __init__(self, db, entry, held):
        self.db, self.entry, self.held = db, entry, Decimal(held)
        self.cfg = guards.TrimConfig()
        self.user_id = uuid.uuid4()
        self.guard = DiscordPositionGuard(
            user_id=self.user_id, symbol="MSFT", option_strike=Decimal("500"),
            option_right=OptionRight.CALL.value, option_expiry=None,
            sell_count=0, entry_price=entry,
        )
        db.add(self.guard); db.flush()

    def alert(self, mark):
        """One exit alert. Returns what left the position right now."""
        plan = guards.plan_exit(self.guard, self.held, Decimal(mark), self.cfg)
        if plan.new_stop_price is not None:
            self.guard.stop_price = plan.new_stop_price
        sold = Decimal(0)
        if plan.exit_style == guards.TRAIL and plan.sell_qty > 0:
            guards.arm_trail(self.guard, plan.sell_qty, plan.trail_amount, Decimal(mark))
        else:
            sold = plan.sell_qty
            self.held -= sold
        if plan.retire:
            guards.retire(self.db, self.guard, plan.note)
        return plan, sold

    def tick(self, mark):
        """One poller tick. Returns what the enforcer sold."""
        sold = []
        # trail_qty is a slice OF the position, not extra on top of it.
        stops.enforce(self.db, self.user_id, _Adapter(_Pos(self.held, mark)),
                      lambda p, g, q: sold.append(Decimal(str(q))))
        if sold:
            self.held = max(Decimal(0), self.held - sold[0])
        return sold[0] if sold else Decimal(0)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    with Session(eng) as s:
        yield s


# ── the spec's sequence, on a cheap contract so every exit is immediate ──────

def test_the_full_ladder_walks_four_contracts_to_zero(db):
    L = _Ladder(db, CHEAP, 4)

    plan, sold = L.alert("1.00")                 # +100%, clears the gate
    assert (plan.rung, sold, L.held) == (1, Decimal(2), Decimal(2))
    assert L.guard.stop_price == Decimal("0.37")    # 25% below 0.50

    plan, sold = L.alert("1.10")
    assert (plan.rung, sold, L.held) == (2, Decimal(1), Decimal(1))
    assert L.guard.stop_price == CHEAP               # break-even

    plan, sold = L.alert("1.20")
    assert (plan.rung, sold, L.held) == (3, Decimal(1), Decimal(0))
    assert L.guard.closed_at is not None             # nothing left, guard retired


# ── the same sequence on an expensive contract, which trails out ─────────────

def test_an_expensive_contract_leaves_via_its_trailing_stops(db):
    L = _Ladder(db, ENTRY, 4)

    plan, sold = L.alert("3.00")                 # +50%
    assert (sold, L.held) == (Decimal(2), Decimal(2))       # 1st trim is always market
    assert L.guard.stop_price == Decimal("1.50")

    plan, sold = L.alert("3.20")
    assert plan.exit_style == guards.TRAIL
    assert sold == Decimal(0)                    # nothing yet — it's riding
    assert L.held == Decimal(2)                  # still held until the trail fires
    assert L.guard.trail_qty == Decimal(1)       # 1 of those 2 is earmarked
    assert L.guard.stop_price == ENTRY           # break-even on the rest

    assert L.tick("3.10") == Decimal(0)          # gave back 0.10 — inside the trail
    assert L.tick("2.94") == Decimal(1)          # gave back 0.26 — out
    assert L.held == Decimal(1)

    plan, _ = L.alert("3.10")
    assert plan.rung == 3
    assert plan.sell_qty == Decimal(1)           # the whole remainder
    assert L.tick("2.80") == Decimal(1)          # trails out
    assert L.guard.closed_at is not None


# ── the stop is what protects the remainder between alerts ──────────────────

def test_the_first_trims_stop_protects_the_remainder(db):
    L = _Ladder(db, CHEAP, 4)
    L.alert("1.00")
    assert L.guard.stop_price == Decimal("0.37")

    assert L.tick("0.40") == Decimal(0)          # above the stop — holds
    assert L.tick("0.36") == Decimal(2)          # broke it — the rest goes
    assert L.guard.closed_at is not None


# ── the gate, in sequence ───────────────────────────────────────────────────

def test_an_alert_under_the_gate_sells_nothing_but_still_advances(db):
    L = _Ladder(db, CHEAP, 4)                    # cheap → rung 2 sells at market

    plan, sold = L.alert("0.52")                 # +4%, under the gate
    assert (plan.rung, sold, L.held) == (1, Decimal(0), Decimal(4))
    assert L.guard.stop_price == Decimal("0.37")   # protected even so

    plan, sold = L.alert("0.52")                 # rung 2 has no gate
    assert (plan.rung, sold, L.held) == (2, Decimal(2), Decimal(2))


def test_an_underwater_ladder_never_stops_itself_out(db):
    """The production failure: bought at 2.30, marking 1.95. Rung 1's stop sits
    at 1.725, safely under the mark — but rung 2 wants break-even at 2.30, which
    is ABOVE it. Setting that would be breached on the spot and flatten
    everything, which is exactly what happened in production."""
    L = _Ladder(db, Decimal("2.30"), 4)

    _, sold = L.alert("1.95")                    # under the gate
    assert (sold, L.held) == (Decimal(0), Decimal(4))
    assert L.guard.stop_price == Decimal("1.72")    # a real stop, below the mark

    plan, sold = L.alert("1.95")                 # rung 2 trims
    assert plan.sell_qty == Decimal(2)           # the trim still happens
    assert L.guard.stop_price == Decimal("1.72")    # break-even REFUSED; 1.725 holds

    assert L.tick("1.95") == Decimal(0)          # and the enforcer exits nothing
    assert L.guard.closed_at is None


def test_the_final_rung_takes_everything_not_half(db):
    """Reaching rung 3 with more than one contract left. Both walkthroughs above
    arrive there holding exactly 1, where "half" and "all" are the same number —
    so neither would notice the last rung quietly trimming instead of exiting."""
    L = _Ladder(db, CHEAP, 8)

    _, sold = L.alert("0.52")                    # under the gate — nothing sold
    assert (sold, L.held) == (Decimal(0), Decimal(8))

    _, sold = L.alert("1.00")                    # rung 2 halves
    assert (sold, L.held) == (Decimal(4), Decimal(4))

    _, sold = L.alert("1.00")                    # rung 3 must take all 4
    assert (sold, L.held) == (Decimal(4), Decimal(0))
    assert L.guard.closed_at is not None
