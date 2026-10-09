"""max_per_contract as a trim-to-budget cap (copy_engine.trim_to_contract_cap).

The cap is a dollar budget on an OPTION order: fit the most whole contracts whose
total value (premium x 100 x qty) stays within it, skip only when not even one
fits. Shared by the copy engine and the Discord ceiling.
"""
import os
import sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.copy_engine import trim_to_contract_cap as trim


def test_within_budget_is_unchanged():
    # 2 contracts x $200 = $400 <= $500
    assert trim(D("2.00"), D("2"), D("500")) == D("2")


def test_exactly_on_the_budget_is_unchanged():
    # 2 x $250 = $500, strictly-over only trims
    assert trim(D("2.50"), D("2"), D("500")) == D("2")


def test_over_budget_trims_to_floor():
    # 10 x $200 = $2000 -> floor(500 / 200) = 2
    assert trim(D("2.00"), D("10"), D("500")) == D("2")


def test_trims_with_a_fractional_floor():
    # 5 x $190 = $950 -> floor(500 / 190) = 2 (2.63 floored)
    assert trim(D("1.90"), D("5"), D("500")) == D("2")


def test_a_single_contract_over_budget_gives_zero():
    # one $5000 contract, $500 budget -> floor(500 / 5000) = 0 (caller skips)
    assert trim(D("50.00"), D("1"), D("500")) == D("0")


def test_no_cap_is_unchanged():
    assert trim(D("2.00"), D("10"), None) == D("10")


def test_unpriced_is_unchanged_not_free():
    assert trim(None, D("10"), D("500")) == D("10")


def test_zero_quantity_is_unchanged():
    assert trim(D("2.00"), D("0"), D("500")) == D("0")


def test_none_quantity_is_none():
    assert trim(D("2.00"), None, D("500")) is None


def test_zero_price_is_unchanged():
    assert trim(D("0"), D("10"), D("500")) == D("10")


def test_trim_never_exceeds_the_request():
    # a tiny budget still can't buy more than the trader opened
    assert trim(D("0.05"), D("3"), D("500")) == D("3")   # 3 x $5 = $15 <= $500
    assert trim(D("0.05"), D("3"), D("8")) == D("1")     # floor(8 / 5) = 1, < 3
