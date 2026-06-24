"""Backfill: correct orders wrongly flagged fanned_out_to_subscribers=True.

Bug (now fixed in copy_engine.fanout_threadsafe): when a trader placed an
order DIRECTLY in their broker while copy trading was PAUSED, the listener
observed it and unconditionally set fanned_out_to_subscribers=True — even
though fanout_async no-op'd (copy was off). Those orders then show up under
the trader's "All Orders" (copy-on) tab instead of "My Orders" (copy-off).

This script reconstructs each trader's copy ON/OFF timeline from the audit
log (trader.copy_paused / trader.copy_resumed) and flips fanned_out=True →
False for any order CREATED during a paused (copy-off) window.

Usage (from backend/):
    .venv/bin/python scripts/fix_fanned_out_flag.py            # dry run
    .venv/bin/python scripts/fix_fanned_out_flag.py --apply    # write
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
from app.models.audit_log import AuditLog
from app.models.order import Order

APPLY = "--apply" in sys.argv


def _paused_intervals(events):
    """events = [(created_at, action)] sorted asc. Returns list of
    (start, end|None) windows during which copy was OFF (paused)."""
    intervals = []
    open_start = None
    for ts, action in events:
        if action == "trader.copy_paused" and open_start is None:
            open_start = ts
        elif action == "trader.copy_resumed" and open_start is not None:
            intervals.append((open_start, ts))
            open_start = None
    if open_start is not None:
        intervals.append((open_start, None))  # still paused → open-ended
    return intervals


def _in_any(ts, intervals):
    for start, end in intervals:
        if ts >= start and (end is None or ts < end):
            return True
    return False


def main() -> int:
    with SessionLocal() as db:
        # Per-trader copy on/off timeline from the audit log.
        events_by_trader = defaultdict(list)
        for a in db.execute(
            select(AuditLog)
            .where(AuditLog.action.in_(["trader.copy_paused", "trader.copy_resumed"]))
            .order_by(AuditLog.created_at.asc())
        ).scalars():
            events_by_trader[a.actor_user_id].append((a.created_at, a.action))

        to_flip = []
        for trader_id, events in events_by_trader.items():
            intervals = _paused_intervals(events)
            if not intervals:
                continue
            rows = db.execute(
                select(Order).where(
                    Order.user_id == trader_id,
                    Order.fanned_out_to_subscribers.is_(True),
                )
            ).scalars().all()
            for o in rows:
                if _in_any(o.created_at, intervals):
                    to_flip.append(o)

        print("=" * 60)
        print("FANNED-OUT FLAG BACKFILL" + ("  [APPLY]" if APPLY else "  [DRY RUN]"))
        print("=" * 60)
        print(f"  Traders with copy on/off history : {len(events_by_trader)}")
        print(f"  Orders to flip True -> False     : {len(to_flip)}")
        if to_flip:
            print("  Sample (up to 8):")
            for o in to_flip[:8]:
                print(f"    {o.created_at}  {o.symbol:6}  {o.status.value}")
        print("=" * 60)

        if not to_flip:
            print("Nothing to fix.")
            return 0
        if not APPLY:
            print("DRY RUN — no changes. Re-run with --apply to write.")
            return 0

        for o in to_flip:
            o.fanned_out_to_subscribers = False
        db.commit()
        print(f"Updated {len(to_flip)} orders -> fanned_out_to_subscribers=False")
        return 0


if __name__ == "__main__":
    sys.exit(main())
