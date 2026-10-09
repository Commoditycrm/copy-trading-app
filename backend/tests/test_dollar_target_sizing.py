"""Unit tests for dollar-target opening-mirror sizing.

Covers the pure sizing helper used by the copy engine when a subscriber sizes
each fresh opening entry to a fixed dollar budget instead of the multiplier.
"""
from decimal import Decimal

from app.services.copy_engine import dollar_target_quantity


def test_option_fits_budget_floors_down():
    # $4.20 premium -> $420 per contract. $500 budget -> 1 contract (not 1.19).
    assert dollar_target_quantity(Decimal("4.20"), Decimal("500"), True) == Decimal("1")


def test_option_larger_budget():
    # $4.20 premium -> $420/contract. $5,000 budget -> 11 contracts ($4,620).
    assert dollar_target_quantity(Decimal("4.20"), Decimal("5000"), True) == Decimal("11")


def test_option_single_contract_over_budget_is_zero():
    # One $6.00 contract = $600 > $500 budget -> 0 (caller skips the trade).
    assert dollar_target_quantity(Decimal("6.00"), Decimal("500"), True) == Decimal("0")


def test_stock_uses_share_price():
    # Stocks: price is per share, no x100. $50 share, $500 budget -> 10 shares.
    assert dollar_target_quantity(Decimal("50"), Decimal("500"), False) == Decimal("10")


def test_unpriceable_returns_none():
    # No price -> None, so the caller skips rather than treating it as free or
    # falling back to a multiplier that could overshoot the budget.
    assert dollar_target_quantity(None, Decimal("500"), True) is None


def test_non_positive_price_returns_none():
    assert dollar_target_quantity(Decimal("0"), Decimal("500"), True) is None


def test_no_budget_returns_none():
    assert dollar_target_quantity(Decimal("4.20"), None, True) is None
