"""Unit tests for the calendar's TODAY cell under the OVERNIGHT-RESET rule.

The calendar shows *daily* P&L, not cumulative. For each day:

    daily P&L = realized(day) + that day's unrealized SWING

where the swing is measured from the PRIOR CLOSE — yesterday's captured
end-of-day unrealized — NOT from the position's entry. So a position carried
overnight locks yesterday's swing into yesterday and starts today's from zero.

``pnl.today_live_cell`` is the pure helper for TODAY's live cell:

    day_unrealized = live_unrealized − prior_close_eod
    marked         = realized_today + day_unrealized

These tests encode the spec's worked example (Day 1 +$10, Day 2 +$5 not +$15)
and its edge cases. Past-day reconstruction (settled days) is covered by
test_snaptrade_marked.py; the realized FIFO (partial closes, options ×100,
multiple lots) by the realized_pnl_by_day tests.

Pure-logic tests — no DB or broker needed.

Run standalone:  .venv/bin/python tests/test_calendar_unrealized.py
Or under pytest: pytest tests/test_calendar_unrealized.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pnl import today_live_cell


def test_day1_open_shows_full_move():
    """Spec Day 1: buy @100, now $110. Opened today, so the prior (flat) close
    is 0 → today shows the full +$10."""
    marked, unreal = today_live_cell(Decimal("0"), Decimal("10"), Decimal("0"))
    assert unreal == Decimal("10")
    assert marked == Decimal("10")


def test_day2_carried_resets_from_prior_close():
    """Spec Day 2 (THE key case): carried overnight, prior close +$10, now $115
    → cumulative unrealized $15, but today's swing is $15 − $10 = +$5, NOT +$15.
    Yesterday's +$10 stays locked on Day 1."""
    marked, unreal = today_live_cell(Decimal("0"), Decimal("15"), Decimal("10"))
    assert unreal == Decimal("5")
    assert marked == Decimal("5")


def test_close_on_day2_books_only_todays_move():
    """Position closed today at $115: realized +$15 booked today, position now
    flat (live 0), prior close was +$10 → day swing 0 − 10 = −10, so today's
    total = 15 − 10 = +$5 (the day's move only; Day 1 keeps its +$10)."""
    marked, unreal = today_live_cell(Decimal("15"), Decimal("0"), Decimal("10"))
    assert unreal == Decimal("-10")
    assert marked == Decimal("5")


def test_open_and_closed_same_day():
    """Opened and fully closed today: prior close 0, flat now (live 0), realized
    +$500 → marked 500, unrealized 0."""
    marked, unreal = today_live_cell(Decimal("500"), Decimal("0"), Decimal("0"))
    assert unreal == Decimal("0")
    assert marked == Decimal("500")


def test_profit_to_loss_across_days():
    """Carried position that was +$400 at yesterday's close and is +$20 now →
    today gave back $380; no closes → marked −$380."""
    marked, unreal = today_live_cell(Decimal("0"), Decimal("20"), Decimal("400"))
    assert unreal == Decimal("-380")
    assert marked == Decimal("-380")


def test_realized_plus_carried_open():
    """Both on one day: realized +$200 from closes AND a carried position that
    moved +$100→+$150 overnight (prior close 100) → day swing +$50, marked
    200 + 50 = +$250."""
    marked, unreal = today_live_cell(Decimal("200"), Decimal("150"), Decimal("100"))
    assert unreal == Decimal("50")
    assert marked == Decimal("250")


def test_multiple_days_telescope_to_total():
    """Buy @100; Day1 close +10, Day2 close +15 then sell. Each day books only
    its own move, and the days sum to the true gain (+15)."""
    d1_marked, _ = today_live_cell(Decimal("0"), Decimal("10"), Decimal("0"))   # +10
    d2_marked, _ = today_live_cell(Decimal("15"), Decimal("0"), Decimal("10"))  # +5
    assert d1_marked == Decimal("10")
    assert d2_marked == Decimal("5")
    assert d1_marked + d2_marked == Decimal("15")   # matches 100 → 115


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll calendar today-cell (overnight-reset) tests passed.")
