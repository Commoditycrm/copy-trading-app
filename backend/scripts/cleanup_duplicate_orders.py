"""One-off cleanup: collapse duplicate order rows created by repeated
broker reconnects.

Background
----------
Deleting a broker SET NULLs its orders' broker_account_id (they survive as
"orphans"). The old fills_sync dedup only matched within the current
account, so reconnecting re-imported the whole history as NEW rows. Result:
the same broker_order_id appears on several Order rows. Those duplicates
inflate the Order-History counts AND realized P&L (each copy carries its
own fills).

This script keeps ONE row per (user_id, broker_order_id) and deletes the
extras. Fills cascade-delete with their order. Any order that referenced a
deleted duplicate as parent_order_id / bracket_parent_id is re-pointed to
the survivor first, so no linkage is silently lost.

Keeper selection (most-canonical wins):
  1. linked to a live broker account (broker_account_id NOT NULL)
  2. then the most fills
  3. then the most recently synced (created_at)

Usage (from backend/):
    .venv/bin/python scripts/cleanup_duplicate_orders.py            # dry run
    .venv/bin/python scripts/cleanup_duplicate_orders.py --apply    # delete
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from sqlalchemy import func, select, update

from app.database import SessionLocal
from app.models.order import Order

APPLY = "--apply" in sys.argv


def _keeper_score(o: Order):
    """Higher tuple = better keeper."""
    return (
        1 if o.broker_account_id is not None else 0,
        len(o.fills),
        o.created_at,
    )


def main() -> int:
    with SessionLocal() as db:
        total_before = db.execute(select(func.count()).select_from(Order)).scalar()

        dup_keys = db.execute(
            select(Order.user_id, Order.broker_order_id)
            .where(Order.broker_order_id.isnot(None))
            .group_by(Order.user_id, Order.broker_order_id)
            .having(func.count() > 1)
        ).all()

        losers: list[Order] = []
        repoint: list[tuple[str, str]] = []  # (loser_id, keeper_id)
        fills_removed = 0

        for user_id, boid in dup_keys:
            rows = db.execute(
                select(Order).where(
                    Order.user_id == user_id,
                    Order.broker_order_id == boid,
                )
            ).scalars().all()
            rows.sort(key=_keeper_score, reverse=True)
            keeper, group_losers = rows[0], rows[1:]
            for lo in group_losers:
                losers.append(lo)
                fills_removed += len(lo.fills)
                repoint.append((str(lo.id), str(keeper.id)))

        print("=" * 60)
        print("DUPLICATE ORDER CLEANUP" + ("  [APPLY]" if APPLY else "  [DRY RUN]"))
        print("=" * 60)
        print(f"  Duplicate (user, broker_order_id) groups : {len(dup_keys)}")
        print(f"  Duplicate order rows to delete           : {len(losers)}")
        print(f"  Fills cascade-deleted with them          : {fills_removed}")
        print(f"  Order rows: {total_before}  ->  {total_before - len(losers)}")
        print("=" * 60)

        if not losers:
            print("Nothing to clean.")
            return 0

        if not APPLY:
            print("DRY RUN — no changes made. Re-run with --apply to delete.")
            return 0

        # Re-point any child linkage from a loser to its keeper, then delete.
        for loser_id, keeper_id in repoint:
            db.execute(
                update(Order).where(Order.parent_order_id == loser_id)
                .values(parent_order_id=keeper_id)
            )
            db.execute(
                update(Order).where(Order.bracket_parent_id == loser_id)
                .values(bracket_parent_id=keeper_id)
            )
        for lo in losers:
            db.delete(lo)  # fills cascade
        db.commit()

        total_after = db.execute(select(func.count()).select_from(Order)).scalar()
        print(f"DELETED {len(losers)} duplicates. Order rows now: {total_after}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
