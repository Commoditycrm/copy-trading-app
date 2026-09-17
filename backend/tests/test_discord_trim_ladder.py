"""The three-rung exit ladder for Discord positions.

    1st alert — only above the profit gate: sell half, stop the rest below entry
    2nd alert — sell half of what's left, lift that stop to break-even
    3rd alert — exit everything left

Every level is measured from the ENTRY price, never the live mark, so these
tests fix the mark independently of entry to prove the levels don't drift with
it. The rung always advances, even when a trim does nothing.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

EXP = date(2026, 10, 16)
CFG = guards.TrimConfig()          # gate 20%, stop 25%, threshold 0.90, trail 0.25


def _guard(entry="2.00", rung=0):
    return DiscordPositionGuard(
        user_id=uuid.uuid4(), symbol="MSFT", option_strike=Decimal("100"),
        option_right=OptionRight.CALL.value, option_expiry=EXP,
        sell_count=rung, entry_price=Decimal(entry) if entry else None,
    )


# ── rung 1: the profit gate ──────────────────────────────────────────────────

def test_first_trim_sells_half_when_well_in_profit():
    g = _guard(entry="2.00")
    plan = guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG)   # +50%

    assert plan.rung == 1
    assert plan.sell_qty == Decimal(2)
    assert plan.exit_style == guards.MARKET
    assert plan.new_stop_price == Decimal("1.50")                  # 25% below 2.00
    assert plan.retire is False


def test_first_trim_sells_nothing_below_the_gate_but_still_protects():
    """The gate decides whether to SELL, not whether to protect. An alert that
    arrives early must not leave the position unstopped until the next one."""
    g = _guard(entry="2.00")
    plan = guards.plan_exit(g, Decimal(4), Decimal("2.20"), CFG)   # +10%

    assert plan.sell_qty == Decimal(0)
    assert plan.new_stop_price == Decimal("1.50")                  # 25% below entry
    assert "under the" in plan.note


def test_a_skipped_first_trim_still_burns_the_rung():
    """The trader's next alert is their second, whatever the first one did.
    Otherwise a flat position could take unlimited 'first' alerts."""
    g = _guard(entry="2.00")
    guards.plan_exit(g, Decimal(4), Decimal("2.10"), CFG)          # below gate
    assert g.sell_count == 1

    plan = guards.plan_exit(g, Decimal(4), Decimal("2.10"), CFG)
    assert plan.rung == 2
    assert plan.sell_qty == Decimal(2)                             # rung 2 has no gate


def test_exactly_at_the_gate_does_trim():
    """The gate is inclusive: "market >= 1.2 x fill" trims AT the threshold."""
    g = _guard(entry="2.00")
    plan = guards.plan_exit(g, Decimal(4), Decimal("2.40"), CFG)   # exactly +20%
    assert plan.sell_qty == Decimal(2)


def test_just_under_the_gate_does_not_trim():
    g = _guard(entry="2.00")
    plan = guards.plan_exit(g, Decimal(4), Decimal("2.39"), CFG)
    assert plan.sell_qty == Decimal(0)


def test_first_trim_cannot_measure_without_a_reference():
    """No entry price or no mark means the gate can't be evaluated. Skip rather
    than guess — selling on an unmeasured position is the expensive mistake."""
    assert guards.plan_exit(_guard(entry=None), Decimal(4), Decimal("3.00"), CFG).sell_qty == 0
    assert guards.plan_exit(_guard(entry="2.00"), Decimal(4), None, CFG).sell_qty == 0


# ── rung 2: halve again, stop to break-even ──────────────────────────────────

def test_second_trim_halves_the_remainder_and_moves_stop_to_break_even():
    g = _guard(entry="2.00", rung=1)
    plan = guards.plan_exit(g, Decimal(2), Decimal("3.00"), CFG)

    assert plan.rung == 2
    assert plan.sell_qty == Decimal(1)
    assert plan.new_stop_price == Decimal("2.00")                  # entry exactly
    assert plan.retire is False


def test_second_trim_has_no_profit_gate():
    """Only the first rung is gated. A trader trimming into a loss still trims."""
    g = _guard(entry="2.00", rung=1)
    plan = guards.plan_exit(g, Decimal(2), Decimal("1.00"), CFG)
    assert plan.sell_qty == Decimal(1)


# ── exit style: trail an expensive contract, market a cheap one ──────────────

def test_an_expensive_contract_exits_on_a_trailing_give_back():
    g = _guard(entry="1.50", rung=1)                               # above 0.90
    plan = guards.plan_exit(g, Decimal(2), Decimal("3.00"), CFG)

    assert plan.exit_style == guards.TRAIL
    assert plan.trail_amount == Decimal("0.25")


def test_a_cheap_contract_exits_at_market():
    g = _guard(entry="0.50", rung=1)                               # below 0.90
    plan = guards.plan_exit(g, Decimal(2), Decimal("3.00"), CFG)

    assert plan.exit_style == guards.MARKET
    assert plan.trail_amount is None


def test_the_threshold_is_strict():
    """Exactly at the threshold is not above it."""
    g = _guard(entry="0.90", rung=1)
    assert guards.plan_exit(g, Decimal(2), Decimal("3"), CFG).exit_style == guards.MARKET


def test_the_thresholds_are_configurable():
    cfg = guards.TrimConfig(
        profit_gate_pct=Decimal("5"), stop_pct=Decimal("10"),
        price_threshold=Decimal("5.00"), trail_amount=Decimal("1.00"),
    )
    g = _guard(entry="2.00")
    plan = guards.plan_exit(g, Decimal(4), Decimal("2.20"), cfg)   # +10%, over a 5% gate
    assert plan.sell_qty == Decimal(2)
    assert plan.new_stop_price == Decimal("1.80")                  # 10% below entry

    g2 = _guard(entry="2.00", rung=1)
    # 2.00 is under a 5.00 threshold, so this one goes to market.
    assert guards.plan_exit(g2, Decimal(2), Decimal("3"), cfg).exit_style == guards.MARKET


# ── rung 3: everything left ──────────────────────────────────────────────────

def test_third_trim_exits_the_whole_remainder():
    g = _guard(entry="0.50", rung=2)
    plan = guards.plan_exit(g, Decimal(3), Decimal("1.00"), CFG)

    assert plan.rung == 3
    assert plan.sell_qty == Decimal(3)
    assert plan.new_stop_price is None
    assert plan.retire is True


def test_a_trailing_third_trim_does_not_retire_until_it_fills():
    """The slice hasn't left yet — retiring now would drop the guard that owns
    the trailing exit still waiting to fire."""
    g = _guard(entry="2.00", rung=2)
    plan = guards.plan_exit(g, Decimal(3), Decimal("4.00"), CFG)

    assert plan.exit_style == guards.TRAIL
    assert plan.retire is False


def test_further_alerts_keep_exiting():
    g = _guard(entry="0.50", rung=5)
    assert guards.plan_exit(g, Decimal(2), Decimal("1"), CFG).sell_qty == Decimal(2)


# ── rounding ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("held,expected", [(5, 3), (4, 2), (3, 2), (2, 1)])
def test_half_rounds_up_to_whole_contracts(held, expected):
    g = _guard(entry="2.00", rung=1)
    assert guards.plan_exit(g, Decimal(held), Decimal("3.00"), CFG).sell_qty == Decimal(expected)


def test_a_single_contract_exits_whole_and_retires():
    """Half of one rounds to one. There's no remainder to protect, so this is a
    close and the guard must not be left holding a stop on nothing."""
    g = _guard(entry="2.00", rung=0)                      # rung 1 → market exit
    plan = guards.plan_exit(g, Decimal(1), Decimal("3.00"), CFG)

    assert plan.sell_qty == Decimal(1)
    assert plan.new_stop_price is None
    assert plan.retire is True


def test_a_single_contract_leaving_on_a_trail_stays_alive():
    """It sells everything, but not YET — the guard owns the pending trailing
    exit, so retiring here would drop the thing still waiting to fire."""
    g = _guard(entry="2.00", rung=1)                      # rung 2 → trails
    plan = guards.plan_exit(g, Decimal(1), Decimal("3.00"), CFG)

    assert plan.sell_qty == Decimal(1)
    assert plan.exit_style == guards.TRAIL
    assert plan.retire is False


def test_an_empty_position_retires_without_selling():
    g = _guard(entry="2.00", rung=1)
    plan = guards.plan_exit(g, Decimal(0), Decimal("3.00"), CFG)
    assert plan.sell_qty == Decimal(0)
    assert plan.retire is True


# ── levels key off entry, not the mark ───────────────────────────────────────

def test_stop_levels_ignore_the_live_mark():
    """Same entry, wildly different marks — the stop must not move."""
    stops = []
    for mark in ("3.00", "8.00", "20.00"):
        g = _guard(entry="2.00")
        stops.append(guards.plan_exit(g, Decimal(4), Decimal(mark), CFG).new_stop_price)
    assert stops == [Decimal("1.50")] * 3


def test_the_ladder_survives_a_restart():
    """Rung and levels live on the row, not in memory."""
    g = _guard(entry="2.00")
    guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG)
    assert g.sell_count == 1
    assert g.entry_price == Decimal("2.00")


# ── an exit whose order never placed ─────────────────────────────────────────

def test_a_rejected_exit_gives_its_rung_back():
    g = _guard(entry="2.00")
    guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG)
    assert g.sell_count == 1

    guards.rollback_exit(g)
    assert g.sell_count == 0
    assert g.trail_qty is None

    # The retry is rung one again, not rung two.
    assert guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG).rung == 1


def test_rollback_never_goes_negative():
    g = _guard(entry="2.00", rung=0)
    guards.rollback_exit(g)
    assert g.sell_count == 0


# ── a stop must never fire the moment it is set ──────────────────────────────

def test_a_break_even_stop_is_not_set_on_an_underwater_position():
    """Bought at 2.30, now marking 1.95. "Stop at break-even" here would be a
    stop already breached — the enforcer would flatten the whole position on its
    next tick, turning a trim into a liquidation."""
    g = _guard(entry="2.30", rung=1)
    plan = guards.plan_exit(g, Decimal(4), Decimal("1.95"), CFG)

    assert plan.sell_qty == Decimal(2)          # the trim itself still happens
    assert plan.new_stop_price is None          # but no self-triggering stop


def test_an_underwater_trim_leaves_an_existing_stop_alone():
    g = _guard(entry="2.30", rung=1)
    g.stop_price = Decimal("1.725")             # set by rung 1
    plan = guards.plan_exit(g, Decimal(4), Decimal("1.95"), CFG)

    assert plan.new_stop_price is None
    assert g.stop_price == Decimal("1.725")     # the lower, still-valid stop holds


def test_a_break_even_stop_is_set_when_the_position_is_above_it():
    g = _guard(entry="2.00", rung=1)
    plan = guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG)
    assert plan.new_stop_price == Decimal("2.00")


def test_a_stop_exactly_at_the_mark_is_not_armed():
    """Equal counts as breached — the enforcer exits at <= stop."""
    g = _guard(entry="2.00", rung=1)
    assert guards.plan_exit(g, Decimal(4), Decimal("2.00"), CFG).new_stop_price is None
