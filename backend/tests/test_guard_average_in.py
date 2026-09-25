"""Averaging down moves the ladder's reference; an ordinary add must not.

The spec's sequence, and what the guard has to read after each step:

    4  @ 0.68                       entry 0.6800
    +4 @ 0.40  -> 8 held            entry 0.5400
    +8 @ 0.23  -> 16 held           entry 0.3850

Holding 0.68 throughout puts the -25% stop at 0.5100 — ABOVE the real cost of
0.3850, so it exits a position that is in profit — and the +20% trim gate at
0.8160, which needs +112% over real cost and so never fires.
"""
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as g


def _guard(entry="0.68"):
    return SimpleNamespace(
        symbol="SPY", entry_price=Decimal(entry) if entry is not None else None
    )


def test_the_spec_sequence_lands_on_the_right_averages():
    gd = _guard()
    assert g.average_in(None, gd, held_qty=Decimal(4), added_qty=Decimal(4),
                        added_price=Decimal("0.40")) == Decimal("0.5400")
    assert g.average_in(None, gd, held_qty=Decimal(8), added_qty=Decimal(8),
                        added_price=Decimal("0.23")) == Decimal("0.3850")


def test_it_is_weighted_by_quantity_not_a_plain_midpoint():
    """A plain (old + new)/2 happens to be right when the add equals the
    holding — which is every case today — so an unweighted formula would pass
    the sequence above and be wrong the moment an add is sized differently."""
    gd = _guard("1.00")
    # 9 held @ 1.00 plus 1 @ 0.50 is 0.95, not the midpoint 0.75.
    assert g.average_in(None, gd, held_qty=Decimal(9), added_qty=Decimal(1),
                        added_price=Decimal("0.50")) == Decimal("0.9500")


def test_averaging_up_also_moves_the_reference():
    """The name says down, but the arithmetic is just a weighted average. An
    add above the reference raises it, which is the honest cost basis."""
    gd = _guard("0.40")
    assert g.average_in(None, gd, held_qty=Decimal(4), added_qty=Decimal(4),
                        added_price=Decimal("0.60")) == Decimal("0.5000")


def test_a_guard_with_no_reference_adopts_the_add(monkeypatch):
    """Nothing to weight against — the add is the only price we know."""
    gd = _guard(None)
    assert g.average_in(None, gd, held_qty=Decimal(4), added_qty=Decimal(4),
                        added_price=Decimal("0.40")) == Decimal("0.40")


@pytest.mark.parametrize("price", [None, Decimal(0), Decimal("-1")])
def test_an_unusable_price_leaves_the_reference_alone(price):
    """A missing or nonsense price must not drag the ladder to zero — every
    level keys off this number."""
    gd = _guard()
    assert g.average_in(None, gd, held_qty=Decimal(4), added_qty=Decimal(4),
                        added_price=price) == Decimal("0.68")
    assert gd.entry_price == Decimal("0.68")


@pytest.mark.parametrize("held,added", [
    (Decimal(0), Decimal(0)),
    (Decimal(4), Decimal(0)),
    # A NEGATIVE add is the one that does damage: 4 held @ 0.68 with -2 @ 0.40
    # computes (2.72 - 0.80) / 2 = 0.96, INFLATING the reference above anything
    # ever paid — which pushes the stop up and the trim gate out of reach.
    (Decimal(4), Decimal(-2)),
])
def test_a_non_positive_quantity_add_changes_nothing(held, added):
    gd = _guard()
    assert g.average_in(None, gd, held_qty=held, added_qty=added,
                        added_price=Decimal("0.40")) == Decimal("0.68")
    assert gd.entry_price == Decimal("0.68")


def test_only_an_averaging_down_alert_re_averages():
    """on_buy holds the reference fixed for ordinary adds on purpose — a
    position that kept averaging UP would otherwise raise its own stop-loss
    under a trader who never asked for that. So the re-average has to be an
    explicit call on the double_up path, not something on_buy started doing."""
    import inspect

    from app.api.discord_sources import _execute_signal

    src = inspect.getsource(_execute_signal)
    assert 'if signal.get("double_up"):' in src
    at = src.index('if signal.get("double_up"):')
    assert "guards.average_in(" in src[at:at + 400]
    # And on_buy itself still refuses to re-price a live position.
    on_buy = inspect.getsource(g.on_buy)
    assert "dormant(db, guard)" in on_buy
