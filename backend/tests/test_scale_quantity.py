"""Copy sizing is WHOLE UNITS ONLY — the scaled quantity is always rounded UP
(ceil) to an integer, even on brokers that support fractional shares.

Rounding UP (product decision 2026-09, replacing the earlier round-DOWN) means a
below-1 multiplier never rounds a trim to zero — the bug that stranded
subscribers on chunked exits. Trade-off: entries are slightly over-sized vs the
exact multiplier (e.g. 1 × 0.25 = 0.25 -> 1). The close-clamp caps closes at what
the subscriber actually holds, so ceil can never over-sell.

Standalone (`.venv/bin/python tests/test_scale_quantity.py`) or under pytest.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce


def _q(trader, mult, fractional=False):
    return ce._scale_quantity(Decimal(str(trader)), Decimal(str(mult)), fractional)


def test_half_multiplier_rounds_up():
    assert _q(3, 0.5) == Decimal("2")      # 1.5 -> 2 (was 1 under round-down)
    assert _q(30, 0.5) == Decimal("15")    # 15.0 -> 15 (whole, unaffected)
    assert _q(300, 0.5) == Decimal("150")
    assert _q(5, 0.5) == Decimal("3")      # 2.5 -> 3


def test_small_size_rounds_up_never_zero():
    # 1 × 0.5 = 0.5 -> 1, and 1 × 0.25 = 0.25 -> 1: a below-1 multiplier can no
    # longer round a trim (or a 1-lot mirror) down to zero.
    assert _q(1, 0.5) == Decimal("1")
    assert _q(1, 0.25) == Decimal("1")


def test_zero_product_is_zero():
    # Only a ZERO product yields no mirror (multiplier 0 == "don't copy").
    assert _q(5, 0) == Decimal("0")


def test_whole_multiplier_is_exact():
    assert _q(3, 1) == Decimal("3")
    assert _q(3, 2) == Decimal("6")


def test_fractional_flag_is_ignored_whole_only():
    # Even when the broker "supports fractional", copies stay whole.
    assert _q(3, 0.5, fractional=True) == Decimal("2")
    assert _q(7, 0.33, fractional=True) == Decimal("3")   # 2.31 -> 3


def test_result_is_always_integral():
    for trader in (1, 2, 3, 7, 10, 33, 100):
        for mult in ("0.1", "0.25", "0.5", "0.67", "1", "1.5", "3"):
            v = _q(trader, mult)
            assert v == v.to_integral_value(), f"{trader}×{mult} -> {v} not whole"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll scale-quantity tests passed.")
