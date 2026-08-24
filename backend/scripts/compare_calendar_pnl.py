"""Read-only breakdown of the NEW calendar P&L, for verification.

Prints, per day, the three components the rebuilt calendar is made of:
  realized   — FIFO over order history (this MUST match the Trades/order page)
  unrealized — Δ from our end-of-day position captures (the open-position swing)
  marked     — what the calendar cell shows = realized + Δunrealized

Use it to sanity-check the rebuild: the `realized` column should line up with
what the order history shows, `marked` is the number on the calendar, and only
today should be flagged `live`. Weekends never appear.

Usage (inside the backend container):
    python scripts/compare_calendar_pnl.py <email> [days_back]
    python scripts/compare_calendar_pnl.py rajeshreddy4088@gmail.com 12

Pure DB read — no broker calls, no writes.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import User, UserRole
from app.services import market_hours, pnl

TZ = "America/New_York"


def _fmt(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    email = sys.argv[1]
    days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 14

    to = market_hours.now_et().date()
    from_ = to - timedelta(days=days_back)

    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email))
        if u is None:
            print(f"no user for {email!r}")
            raise SystemExit(1)
        mirrors_only = u.role == UserRole.SUBSCRIBER

        # Realized backbone (order history) — should match the Trades page.
        realized = pnl.realized_pnl_by_day(
            db, u.id, start=from_, end=to, tz_name=TZ, mirrors_only=mirrors_only
        )
        # The assembled calendar.
        series = pnl.calendar_series(
            db, u.id, from_, to, tz_name=TZ, mirrors_only=mirrors_only
        )

        print(f"\n{email}  role={u.role.value}  {from_} .. {to}  (mirrors_only={mirrors_only})")
        print(f"{'day':12} {'realized':>10} {'unrealized':>11} {'marked':>10} "
              f"{'trades':>7} {'live':>5}")
        print("-" * 62)
        total_real = total_marked = 0.0
        for d in sorted(series):
            c = series[d]
            r = realized.get(d, (None, 0))[0]
            total_real += float(c.realized_pnl)
            total_marked += float(c.marked_pnl)
            print(f"{str(d):12} {_fmt(c.realized_pnl):>10} {_fmt(c.unrealized_pnl):>11} "
                  f"{_fmt(c.marked_pnl):>10} {c.trade_count:>7} "
                  f"{'yes' if c.live else '':>5}")
        print("-" * 62)
        print(f"{'TOTAL':12} {total_real:>10.2f} {'':>11} {total_marked:>10.2f}")


if __name__ == "__main__":
    main()
