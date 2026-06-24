"""One-off backfill: correct orders whose filled_quantity exceeds the order
quantity (the double-counted-fill symptom).

Bug (now fixed in fills_sync): for an order placed through our app that
filled instantly, the place response set filled_quantity (e.g. 1), then
fills_sync ADDED the matching fill activity on top (→ 2). So filled_quantity
ended up GREATER than the order quantity.

The one invariant that is always true: you can never fill MORE than you
ordered. So this script clamps ``filled_quantity`` down to ``quantity`` for
any order where it's larger. It deliberately does NOT touch orders where
filled_quantity <= quantity (those are either correct or genuine partials),
and it doesn't trust raw Fill-row sums (which can themselves be duplicated).

Usage (from backend/):
    .venv/bin/python scripts/fix_double_counted_fills.py            # dry run
    .venv/bin/python scripts/fix_double_counted_fills.py --apply    # write
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from sqlalchemy import select

from app.database import SessionLocal
from app.models.order import Order

APPLY = "--apply" in sys.argv


def main() -> int:
    with SessionLocal() as db:
        rows = db.execute(
            select(Order).where(
                Order.filled_quantity > Order.quantity,
                Order.quantity > 0,
            )
        ).scalars().all()

        print("=" * 60)
        print("FILLED-QTY CLAMP BACKFILL" + ("  [APPLY]" if APPLY else "  [DRY RUN]"))
        print("=" * 60)
        print(f"  Orders with filled_quantity > quantity: {len(rows)}")
        for o in rows[:12]:
            print(f"    {o.symbol:6} {o.side.value:4} {o.status.value:10} filled {o.filled_quantity} -> {o.quantity}")
        if len(rows) > 12:
            print(f"    … and {len(rows) - 12} more")
        print("=" * 60)

        if not rows:
            print("Nothing to fix.")
            return 0
        if not APPLY:
            print("DRY RUN — no changes. Re-run with --apply to write.")
            return 0

        for o in rows:
            o.filled_quantity = Decimal(o.quantity)
        db.commit()
        print(f"Clamped {len(rows)} orders to their order quantity.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
