"""Unit tests for the calendar's live-today marked cell (direct Alpaca).

The calendar shows LOCKED marked P&L on settled days (realized + the
unrealized mark-to-market as of that day's close) and, for TODAY, the LIVE
marked P&L — realized-so-far PLUS today's unrealized change — the exact figure
Alpaca's app shows, ticking with the market. Because today's marked value
ALREADY includes realized, it REPLACES the realized-only cell rather than
adding to it. These cover that math (`_today_marked_cell`), including the
day-boundary example: a position up +200 at Monday's close locks +200 into
Monday, so Tuesday shows only its incremental change.

Pure-logic tests — no DB or broker needed.

Run standalone:  .venv/bin/python tests/test_calendar_unrealized.py
Or under pytest: pytest tests/test_calendar_unrealized.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.trades import _today_marked_cell


def test_marked_replaces_realized_and_splits_unrealized():
    """Realized 500, marked 700 → cell shows 700, unrealized = 200 (not 500+200)."""
    cell, unreal = _today_marked_cell(Decimal("700"), Decimal("500"), 6, Decimal("1.4"))
    assert cell == (Decimal("700"), 6, Decimal("1.4"))
    assert unreal == Decimal("200")


def test_all_closed_today_zero_unrealized():
    """Everything closed today → marked == realized, unrealized 0."""
    cell, unreal = _today_marked_cell(Decimal("500"), Decimal("500"), 4, Decimal("1.0"))
    assert cell == (Decimal("500"), 4, Decimal("1.0"))
    assert unreal == Decimal("0")


def test_next_day_incremental_only():
    """Day-boundary case: position was +200 at yesterday's close (locked into
    yesterday). Today no closes (realized 0) and the position ticked 200→300,
    so today's marked is the +100 INCREMENT — not the full 300."""
    cell, unreal = _today_marked_cell(Decimal("100"), Decimal("0"), 0, Decimal("0.2"))
    assert cell == (Decimal("100"), 0, Decimal("0.2"))
    assert unreal == Decimal("100")


def test_open_position_underwater_pulls_today_down():
    """Realized +100 today but open positions marked -380 → today shows -280."""
    cell, unreal = _today_marked_cell(Decimal("-280"), Decimal("100"), 3, None)
    assert cell == (Decimal("-280"), 3, None)
    assert unreal == Decimal("-380")


def test_pct_and_count_pass_through():
    cell, _ = _today_marked_cell(Decimal("42"), Decimal("0"), 0, Decimal("-2.5"))
    assert cell[1] == 0
    assert cell[2] == Decimal("-2.5")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll calendar marked-cell tests passed.")
