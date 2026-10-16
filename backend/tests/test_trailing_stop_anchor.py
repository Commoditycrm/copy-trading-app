"""A trailing stop measures from the market; a fixed stop from the cost basis.

Conflating them is how a trailing stop lands in the wrong place. The two use
different references on purpose:

    fixed     entry x (1 + stop_pct/100)      a return from what was paid
    trailing  peak x (1 - stop_pct/100)       a give-back from the best price

The one case where they coincide is the On Fill stop: at the instant the entry
fills the position has not moved, so the entry IS the high. Everywhere else a
trim fires after the position has RUN, and measuring its trail from the entry
puts the stop a give-back below the COST — on a position up 200%, far under the
market, handing back nearly the whole gain.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as g

ENTRY = Decimal("1.00")
FIXED = g.RungConfig(Decimal(0), Decimal("-25"), Decimal(50))            # -25% of entry
TRAIL = g.RungConfig(Decimal(0), Decimal("-20"), Decimal(50), stop_trail=True)


def test_a_fixed_stop_comes_off_the_cost_basis():
    stop, pct, peak = g.rung_stop(ENTRY, FIXED, Decimal("3.00"))
    assert stop == Decimal("0.75"), "a fixed stop must ignore the market price"
    assert (pct, peak) == (None, None)


def test_a_trailing_stop_comes_off_the_market():
    stop, pct, peak = g.rung_stop(ENTRY, TRAIL, Decimal("3.00"))
    assert (stop, pct, peak) == (Decimal("2.40"), Decimal(20), Decimal("3.00"))


def test_a_trim_with_no_mark_proposes_no_trailing_stop():
    """The bug: with no mark this anchored the trail to the ENTRY, so a
    position up 200% got a stop at 0.80 instead of 2.40 — below its cost."""
    assert g.rung_stop(ENTRY, TRAIL, None) == (None, None, None)


def test_a_proposed_none_leaves_the_existing_stop_alone():
    """No anchor must not mean no protection: whatever already guards the
    position stays, and the next pass with a mark sets the trail."""
    guard = type("G", (), {"stop_price": Decimal("0.90"), "stop_trail_pct": None,
                           "stop_peak": None})()
    g.apply_stop(guard, g.TrimPlan(rung=2, guard=guard, new_stop_price=None))
    assert guard.stop_price == Decimal("0.90")


def test_the_on_fill_stop_may_anchor_to_the_entry():
    """At the fill the entry IS the high — the one place the two coincide."""
    stop, pct, peak = g.rung_stop(ENTRY, TRAIL, None, anchor_to_entry=True)
    assert (stop, pct, peak) == (Decimal("0.80"), Decimal(20), ENTRY)


def test_a_live_mark_always_wins_over_the_entry_anchor():
    stop, _pct, peak = g.rung_stop(ENTRY, TRAIL, Decimal("3.00"), anchor_to_entry=True)
    assert peak == Decimal("3.00") and stop == Decimal("2.40")


@pytest.mark.parametrize("mark", [None, Decimal(0)])
def test_an_unusable_mark_is_no_anchor(mark):
    assert g.rung_stop(ENTRY, TRAIL, mark) == (None, None, None)


def test_a_fixed_stop_is_unaffected_by_a_missing_mark():
    """Only the TRAILING kind needs the market — a fixed stop never did."""
    stop, _pct, _peak = g.rung_stop(ENTRY, FIXED, None)
    assert stop == Decimal("0.75")
