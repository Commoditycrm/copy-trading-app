"""Unit tests for SnapTrade/Webull MARKED daily-P&L reconstruction.

These brokers expose no marked-history series, so the Calendar rebuilds marked
daily P&L from our own end-of-day unrealized captures:

    marked(D) = realized(D) + (eod_unrealized(D) − eod_unrealized(prev capture))

Key properties covered: the Δunrealized terms telescope so a round-trip's
marked total equals its realized total (no double-count); it's forward-only
(days without a prior capture fall back to realized); and it diffs against the
last CAPTURED day, so weekend/holiday gaps attribute the move to the next
session — matching how Alpaca's own app books it.

Pure-logic tests — no DB or broker needed.

Run standalone:  .venv/bin/python tests/test_snaptrade_marked.py
Or under pytest: pytest tests/test_snaptrade_marked.py
"""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pnl import reconstruct_marked_series

D0 = date(2026, 8, 17)  # baseline capture (flat, before the position)
D1 = date(2026, 8, 18)  # open, +200 unrealized at close
D2 = date(2026, 8, 19)  # hold, +300 unrealized at close
D3 = date(2026, 8, 20)  # close for +500 realized, flat at close


def test_roundtrip_marked_sums_to_realized():
    """Open→hold→close: each day books its own contribution, and the marked
    total over the life equals the realized total (200 + 100 + 200 = 500)."""
    realized = {D3: Decimal("500")}                       # closes only on D3
    eod = {D0: Decimal("0"), D1: Decimal("200"), D2: Decimal("300"), D3: Decimal("0")}
    m = reconstruct_marked_series([D1, D2, D3], realized, eod)
    assert m[D1] == Decimal("200")   # 0 + (200 − 0)
    assert m[D2] == Decimal("100")   # 0 + (300 − 200)
    assert m[D3] == Decimal("200")   # 500 + (0 − 300)
    assert m[D1] + m[D2] + m[D3] == Decimal("500")        # telescopes to realized


def test_forward_only_first_capture_falls_back_to_realized():
    """No earlier capture to diff against → realized-only (the day we start)."""
    m = reconstruct_marked_series([D1], {D1: Decimal("50")}, {D1: Decimal("200")})
    assert m[D1] == Decimal("50")


def test_day_without_own_capture_is_realized_only():
    m = reconstruct_marked_series([D1], {D1: Decimal("80")}, {D0: Decimal("0")})
    assert m[D1] == Decimal("80")   # D1 not in eod map


def test_weekend_gap_diffs_against_last_capture():
    """Fri captured, Sat/Sun skipped; Monday diffs against Friday's close."""
    fri, mon = date(2026, 8, 14), date(2026, 8, 17)
    m = reconstruct_marked_series([mon], {mon: Decimal("0")}, {fri: Decimal("100"), mon: Decimal("250")})
    assert m[mon] == Decimal("150")   # 0 + (250 − 100), weekend move booked to Mon


def test_held_only_day_shows_unrealized_swing():
    """No trades that day, position marked up → the mark change is the P&L."""
    m = reconstruct_marked_series([D2], {}, {D1: Decimal("200"), D2: Decimal("300")})
    assert m[D2] == Decimal("100")


def test_underwater_marked_can_flip_a_green_realized_day():
    """Realized +100 but open positions gave back 380 → marked −280."""
    m = reconstruct_marked_series(
        [D2], {D2: Decimal("100")}, {D1: Decimal("400"), D2: Decimal("20")},
    )
    assert m[D2] == Decimal("-280")   # 100 + (20 − 400)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll SnapTrade marked-reconstruction tests passed.")
