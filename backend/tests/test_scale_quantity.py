"""Copy sizing is WHOLE SHARES ONLY — the scaled quantity is always rounded DOWN
to an integer, even on brokers that support fractional shares.

Regression for the QA report: trader bought 3 with a 0.5 multiplier and the
subscriber's copy should be 1 (3 × 0.5 = 1.5 → 1), never 1.5 or 2.

Standalone (`.venv/bin/python tests/test_scale_quantity.py`) or under pytest.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce


def _q(trader, mult, fractional=False):
    return ce._scale_quantity(Decimal(str(trader)), Decimal(str(mult)), fractional)


def test_half_multiplier_rounds_down():
    assert _q(3, 0.5) == Decimal("1")      # the QA case: 1.5 -> 1
    assert _q(30, 0.5) == Decimal("15")
    assert _q(300, 0.5) == Decimal("150")


def test_size_that_rounds_to_zero_is_zero():
    # 1 × 0.5 = 0.5 -> 0 (the fanout skips it as copy.skipped_zero_qty).
    assert _q(1, 0.5) == Decimal("0")


def test_whole_multiplier_is_exact():
    assert _q(3, 1) == Decimal("3")
    assert _q(3, 2) == Decimal("6")


def test_fractional_flag_is_ignored_whole_only():
    # Even when the broker "supports fractional", copies stay whole.
    assert _q(3, 0.5, fractional=True) == Decimal("1")
    assert _q(7, 0.33, fractional=True) == Decimal("2")   # 2.31 -> 2


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
