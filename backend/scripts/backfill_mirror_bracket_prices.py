"""One-off backfill: compute absolute TP/SL prices for subscriber mirror
entries that only carry the copied PERCENT bracket.

Why this is needed
------------------
A copied bracket stamps ``take_profit_pct`` / ``stop_loss_pct`` on the
subscriber's mirror entry; the emulator re-anchors those onto the
subscriber's own fill when it places the exit legs. For an OPTION, the SL
leg can't rest at the broker (Alpaca rejects STOP on options) — it's
enforced by the price monitor, which reads ``entry.stop_loss_price``.

The emulator now persists that absolute price when it places the legs
(see bracket_emulator.emulate_bracket_exits), but entries whose legs were
ALREADY placed before that change keep ``stop_loss_price = NULL`` — so
their copied option SL has no concrete trigger and never fires. This script
backfills those rows using the exact same re-anchor math the emulator uses,
so the monitor can pick them up on the next tick.

Only fills in a price column that is currently NULL (never clobbers an
explicit price) and only when the pct + an anchor price are present.

Usage (from backend/):
    .venv/bin/python scripts/backfill_mirror_bracket_prices.py            # dry run
    .venv/bin/python scripts/backfill_mirror_bracket_prices.py --apply    # write
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.models.order import Order, OrderStatus
from app.services.bracket_emulator import _reanchor_exit_price

APPLY = "--apply" in sys.argv


def main() -> int:
    with SessionLocal() as db:
        # FILLED mirror entries (parent_order_id set, not an exit leg) that
        # carry a copied pct bracket but are missing the matching absolute
        # price.
        rows = db.execute(
            select(Order).where(
                Order.parent_order_id.isnot(None),
                Order.bracket_leg.is_(None),
                Order.is_closing.is_(False),
                Order.status == OrderStatus.FILLED,
                or_(
                    Order.take_profit_pct.isnot(None),
                    Order.stop_loss_pct.isnot(None),
                ),
                or_(
                    Order.take_profit_price.is_(None),
                    Order.stop_loss_price.is_(None),
                ),
            )
        ).scalars().all()

        print("=" * 64)
        print("MIRROR BRACKET PRICE BACKFILL" + ("  [APPLY]" if APPLY else "  [DRY RUN]"))
        print("=" * 64)

        changed = 0
        for o in rows:
            # Fill-first, matching emulate_bracket_exits: the copied % must be
            # re-anchored on what the subscriber actually paid, not the mirror's
            # limit (which can differ enough to push the exits out of reach).
            anchor = o.filled_avg_price or o.limit_price
            if not anchor or anchor <= 0:
                continue
            before = (o.take_profit_price, o.stop_loss_price)
            if o.take_profit_pct is not None and o.take_profit_price is None:
                o.take_profit_price = _reanchor_exit_price(
                    anchor, o.take_profit_pct, o.side, "tp", o.instrument_type
                )
            if o.stop_loss_pct is not None and o.stop_loss_price is None:
                o.stop_loss_price = _reanchor_exit_price(
                    anchor, o.stop_loss_pct, o.side, "sl", o.instrument_type
                )
            after = (o.take_profit_price, o.stop_loss_price)
            if after != before:
                changed += 1
                inst = getattr(o.instrument_type, "value", o.instrument_type)
                print(
                    f"  {o.symbol:6} {o.side.value:4} {str(inst):6} anchor={anchor} "
                    f"tp_pct={o.take_profit_pct} sl_pct={o.stop_loss_pct} "
                    f"-> TP={o.take_profit_price} SL={o.stop_loss_price}"
                )

        print("=" * 64)
        print(f"  Candidate mirror entries : {len(rows)}")
        print(f"  Rows updated             : {changed}")
        print("=" * 64)

        if changed == 0:
            print("Nothing to backfill.")
            return 0
        if not APPLY:
            print("DRY RUN — no changes. Re-run with --apply to write.")
            return 0

        db.commit()
        print(f"Backfilled {changed} mirror entries.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
