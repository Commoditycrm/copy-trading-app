"""Each rung sells a CONFIGURED share of what is still held.

The ladder used to hardcode it — half, half, everything. The sizes are now
settings, and the defaults (50 / 50 / 100) reproduce the old behaviour exactly,
so nobody's live ladder changes shape by upgrading.

"Of what is still held" is the part that makes the rungs compose: 50/50/100
works a position of 4 down as 2, then 1, then 1. Measured against the ORIGINAL
position instead, 50/50/100 would sell 2, then 2, then 4 — an oversell on the
third rung.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as g


@pytest.mark.parametrize("held,pct,want", [
    (Decimal(4), Decimal(50), Decimal(2)),
    (Decimal(4), Decimal(30), Decimal(2)),    # 1.2 -> 2, rounded UP
    (Decimal(10), Decimal(30), Decimal(3)),
    (Decimal(4), Decimal(100), Decimal(4)),
    (Decimal(1), Decimal(50), Decimal(1)),    # never rounds a trim to nothing
])
def test_the_slice_is_a_share_of_what_is_held(held, pct, want):
    assert g._slice(held, pct) == want


def test_it_rounds_up_so_a_trim_is_never_a_no_op():
    """30% of one contract is 0.3. Rounding down would sell nothing while
    still consuming a rung — walking the trader down the ladder without ever
    reducing the position."""
    assert g._slice(Decimal(1), Decimal(30)) == Decimal(1)


def test_it_never_oversells():
    """Rounding up plus a percentage near 100 would otherwise ask for more
    than is held, which the broker rejects outright."""
    assert g._slice(Decimal(3), Decimal(99)) == Decimal(3)
    assert g._slice(Decimal(3), Decimal(150)) == Decimal(3)


@pytest.mark.parametrize("pct", [Decimal(0), Decimal(-50)])
def test_a_non_positive_percentage_sells_nothing(pct):
    """0 is a legitimate way to switch a rung off. A NEGATIVE one is the case
    that does damage: 100 held at -50% computes a sell of -50, and a negative
    quantity is not "sell nothing", it is an order nobody can place."""
    assert g._slice(Decimal(100), pct) == Decimal(0)


def test_nothing_held_sells_nothing():
    assert g._slice(Decimal(0), Decimal(50)) == Decimal(0)


# ── the rungs compose ───────────────────────────────────────────────────────

def _cfg(q1, q2, q3):
    return g.TrimConfig(
        trim1=g.RungConfig(Decimal(0), Decimal(0), Decimal(q1)),
        trim2=g.RungConfig(Decimal(0), Decimal(0), Decimal(q2)),
        trim3=g.RungConfig(Decimal(0), Decimal(0), Decimal(q3)),
        price_threshold=Decimal(100),          # cheap: every rung goes to market
    )


def _walk(cfg, held):
    """Run the whole ladder over one position, returning what each rung sold."""
    from types import SimpleNamespace

    guard = SimpleNamespace(sell_count=0, entry_price=Decimal("1.00"),
                            symbol="SPY", stop_price=None, trail_qty=None)
    sold = []
    for _ in range(3):
        plan = g.plan_exit(guard, held, Decimal("1.00"), cfg)
        sold.append(plan.sell_qty)
        held -= plan.sell_qty
    return sold, held


def test_the_shipped_defaults_reproduce_the_old_ladder():
    """50 / 50 / 100 on a position of 4: two, one, one — exactly what half,
    half, everything did before the size was configurable."""
    sold, left = _walk(_cfg("50", "50", "100"), Decimal(4))
    assert sold == [Decimal(2), Decimal(1), Decimal(1)]
    assert left == Decimal(0)


def test_the_sheets_ladder_works_down_to_nothing():
    """30 / 50 / 100 — the example from the spec."""
    sold, left = _walk(_cfg("30", "50", "100"), Decimal(10))
    assert sold == [Decimal(3), Decimal(4), Decimal(3)]
    assert left == Decimal(0)


def test_the_third_rung_still_exits_the_rest():
    sold, left = _walk(_cfg("50", "50", "100"), Decimal(7))
    assert left == Decimal(0)
    assert sum(sold) == Decimal(7)


# ── the stop is a SIGNED offset from entry ──────────────────────────────────
#
#   -25  ->  entry x 0.75   25% below entry, the usual protective stop
#     0  ->  entry          break-even
#   +10  ->  entry x 1.10   10% ABOVE entry, locking in profit
#
# Before this the value was an unsigned DISTANCE BELOW entry, so the highest a
# stop could ever sit was break-even and "move the stop to +10%" could not be
# expressed at all. Migration e4c9d2a6b183 negated every stored value, so a
# ladder that read 25 now reads -25 and its stop sits exactly where it did.

def _plan_with_stop(stop_pct, entry="1.00", mark="1.50"):
    from types import SimpleNamespace

    cfg = g.TrimConfig(
        trim1=g.RungConfig(Decimal(0), Decimal(str(stop_pct)), Decimal(50)),
        price_threshold=Decimal(100),
    )
    guard = SimpleNamespace(sell_count=0, entry_price=Decimal(entry),
                            symbol="SPY", stop_price=None, trail_qty=None)
    return g.plan_exit(guard, Decimal(4), Decimal(mark), cfg)


@pytest.mark.parametrize("stop_pct,level", [
    ("-25", "0.7500"),      # 25% below entry
    ("-10", "0.9000"),
    ("0", "1.0000"),        # break-even, unchanged
    ("10", "1.1000"),       # ABOVE entry: locks in profit
    ("25", "1.2500"),
])
def test_the_sign_places_the_stop(stop_pct, level):
    assert _plan_with_stop(stop_pct).new_stop_price == Decimal(level)


def test_a_profit_locking_stop_is_now_expressible():
    """The whole reason for the sign. Under the old unsigned field the best a
    2nd trim could do was break-even; it can now hold the remainder at +10%."""
    assert _plan_with_stop("10").new_stop_price > Decimal("1.00")


def test_a_stop_above_the_mark_is_not_armed():
    """Unchanged guard, and it matters more now: a stop at or above the live
    price reads as already breached and would flatten the position on the next
    tick. Returning None leaves whatever stop was already there."""
    assert _plan_with_stop("60", mark="1.50").new_stop_price is None


def test_the_shipped_default_is_negative():
    """25% BELOW entry — the level every existing ladder already had, now
    spelled with the sign that says so."""
    assert g.TrimConfig().trim1.stop_pct == Decimal("-25")


def test_the_api_accepts_a_negative_stop():
    """It used to floor stop fields at 0, so "-25" was a 400 — and -25 is now
    the DEFAULT, so flooring at 0 would reject the shipped ladder."""
    import inspect

    from app.api.discord_sources import _apply_settings

    src = inspect.getsource(_apply_settings)
    for field in ("trim_stop_pct", "trim2_stop_pct", "trim3_stop_pct"):
        assert f'("{field}", "discord_{field}", Decimal(100), _SIGNED)' in src
    # The gates and sizes are NOT signed — a negative there is meaningless.
    assert '("trim_profit_gate_pct", "discord_trim_profit_gate_pct", Decimal(100), _ZERO_OK)' in src
    assert '("trim_qty_pct", "discord_trim_qty_pct", Decimal(100), _ZERO_OK)' in src
