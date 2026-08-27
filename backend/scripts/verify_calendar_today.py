"""Verify the calendar's TODAY cell (realized + live unrealized) for every
subscriber — the same computation the /trades/calendar/pnl endpoint runs.

Read-only: it syncs fills (same as the endpoint) and fetches live positions,
but writes no P&L. Run inside the backend container:

    docker exec -it copy-trading-app-backend-1 \
        python -m scripts.verify_calendar_today

Add an email substring to check a single sub:

    docker exec -it copy-trading-app-backend-1 \
        python -m scripts.verify_calendar_today revathi
"""
import sys
from decimal import Decimal

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import User, UserRole
from app.services import fills_sync, market_hours
from app.services.pnl import calendar_series
# _live_unrealized_today lives in the API module — reuse it verbatim so this
# matches the endpoint exactly (same broker call, same swallow-on-failure).
from app.api.trades import _live_unrealized_today


def money(d):
    if d is None:
        return "     —   "
    return f"{Decimal(str(d)):>10.2f}"


def main() -> None:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else None
    today = market_hours.now_et().date()

    db = SessionLocal()
    try:
        subs = db.execute(
            select(User).where(User.role == UserRole.SUBSCRIBER)
        ).scalars().all()

        print(f"\nCalendar TODAY ({today}) — realized + live unrealized per sub\n")
        print(f"{'email':<40} {'REALIZED':>10} {'UNREAL':>10} {'SHOWN':>10} "
              f"{'trades':>6}  live")
        print("-" * 90)

        n = 0
        for u in subs:
            if needle and needle not in (u.email or "").lower():
                continue
            n += 1

            # Same order of operations as calendar_pnl():
            try:
                fills_sync.sync_user_fills(db, u.id)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()

            live_unreal = _live_unrealized_today(db, u.id)
            series = calendar_series(
                db, u.id, today, today,
                tz_name=None, mirrors_only=True,
                live_today_unrealized=live_unreal,
            )
            cell = series.get(today)
            if cell is None:
                print(f"{u.email:<40} {'(no cell)':>10}")
                continue

            # marked_pnl = realized + Δunrealized (the SHOWN number).
            # realized alone = marked - unrealized swing.
            shown = cell.marked_pnl
            unreal = cell.unrealized_pnl
            realized = (shown - unreal) if (shown is not None and unreal is not None) else shown
            print(f"{u.email:<40} {money(realized)} {money(unreal)} {money(shown)} "
                  f"{cell.trade_count:>6}  {cell.live}")

        print("-" * 90)
        print(f"{n} subscriber(s) checked.\n")
        print("SHOWN = what the calendar TODAY cell displays (realized + live unrealized swing).")
        print("live=True means today used a LIVE broker fetch; live=False fell back to last EOD capture.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
