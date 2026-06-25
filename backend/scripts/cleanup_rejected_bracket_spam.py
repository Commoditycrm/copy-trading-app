"""One-off cleanup: collapse duplicate rejected bracket-exit legs.

A poller-driven reconcile (now fixed) re-attempted copy-bracket exits every
tick for entries whose position was already gone, producing hundreds of
REJECTED exit-leg rows that never reached the broker. This keeps ONE row per
(entry, leg) group — so the reconcile still treats the bracket as "attempted"
and won't recreate it — and deletes the rest.

Only touches rows with bracket_leg set, status=rejected, AND broker_order_id
NULL (i.e. never reached the broker — pure local spam).

Usage (from backend/):
    .venv/bin/python scripts/cleanup_rejected_bracket_spam.py            # dry run
    .venv/bin/python scripts/cleanup_rejected_bracket_spam.py --apply    # delete
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from sqlalchemy import select

from app.database import SessionLocal
from app.models.order import Order, OrderStatus

APPLY = "--apply" in sys.argv


def main() -> int:
    with SessionLocal() as db:
        rows = db.execute(
            select(Order).where(
                Order.bracket_leg.isnot(None),
                Order.status == OrderStatus.REJECTED,
                Order.broker_order_id.is_(None),
            ).order_by(Order.created_at.asc())
        ).scalars().all()

        groups: dict[tuple, list[Order]] = defaultdict(list)
        for o in rows:
            groups[(o.bracket_parent_id, o.bracket_leg)].append(o)

        losers = [o for g in groups.values() for o in g[1:]]  # keep the first (oldest) per group

        print("=" * 60)
        print("REJECTED BRACKET-LEG SPAM CLEANUP" + ("  [APPLY]" if APPLY else "  [DRY RUN]"))
        print("=" * 60)
        print(f"  Total rejected exit-leg rows : {len(rows)}")
        print(f"  Distinct (entry, leg) groups : {len(groups)}  (kept)")
        print(f"  Duplicate rows to delete     : {len(losers)}")
        print("=" * 60)

        if not losers:
            print("Nothing to clean.")
            return 0
        if not APPLY:
            print("DRY RUN — no changes. Re-run with --apply to delete.")
            return 0

        for o in losers:
            db.delete(o)
        db.commit()
        print(f"Deleted {len(losers)} duplicate rejected exit-leg rows.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
