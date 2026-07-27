"""Daily realized-P&L snapshot job.

Freezes each user's per-day realized P&L, computed from the broker's OWN complete
activity feed (broker_pnl.realized_by_day_from_broker), into
daily_realized_pnl_snapshots. The Calendar reads those snapshots instead of
recomputing from our (drift-prone) DB fills, and — because each day is frozen
from whatever broker was connected that day — the Calendar stays correct even
when a user switches brokers over time.

Runs periodically in the worker (see start_pnl_snapshot_job). A rolling window
is refreshed each pass so late-arriving broker activity is picked up; older days,
once frozen, don't change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.services import market_hours
from app.services.broker_pnl import realized_by_day_from_broker
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# Refresh this many days back each pass. Options day-trades settle same day, so a
# ~5-week window comfortably covers any late broker-feed arrivals without
# re-pulling the whole history every time.
SNAPSHOT_WINDOW_DAYS = 35
# How often the worker runs a sweep. Hourly is plenty — the calendar reads frozen
# rows, and intraday freshness for the current day comes from the live fallback.
SNAPSHOT_INTERVAL_S = 3600.0


def store_account_snapshots(db: Session, acct: BrokerAccount, start: date, end: date) -> int:
    """Compute broker-direct realized P&L for [start, end] for ONE account and
    upsert per-day rows keyed by (user_id, day). Returns rows written.

    Two broker-direct sources, picked by capability:
      * SnapTrade exposes a complete trade-activity feed → REALIZED P&L, FIFO'd
        (``realized_by_day_from_broker``). SnapTrade does not expose marked
        equity, so realized is the best we can do there.
      * Alpaca exposes a marked portfolio-history series → MARKED P&L (realized +
        unrealized), which matches Alpaca's own app calendar exactly.
    Brokers with neither keep using the DB calc via the calendar fallback."""
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    if hasattr(adapter, "get_account_activities"):
        daily = realized_by_day_from_broker(adapter, start, end)
        source = "broker_activities"
    elif hasattr(adapter, "marked_pnl_by_day"):
        daily = adapter.marked_pnl_by_day(start, end)
        source = "portfolio_history"
    else:
        return 0
    broker = acct.broker.value if acct.broker else None
    written = 0
    for day, vals in daily.items():
        # SnapTrade yields (pnl, count); Alpaca yields (pnl, count, pct). The
        # daily-return % only exists for Alpaca (portfolio-history); NULL else.
        pnl, count = vals[0], vals[1]
        pct = vals[2] if len(vals) > 2 else None
        stmt = (
            pg_insert(DailyRealizedPnlSnapshot)
            .values(
                user_id=acct.user_id, day=day,
                realized_pnl=Decimal(pnl), trade_count=int(count), pct=pct,
                broker_account_id=acct.id, broker=broker, source=source,
            )
            .on_conflict_do_update(
                constraint="uq_daily_realized_pnl_user_day",
                set_=dict(
                    realized_pnl=Decimal(pnl), trade_count=int(count), pct=pct,
                    broker_account_id=acct.id, broker=broker,
                    source=source, computed_at=market_hours.now_et(),
                ),
            )
        )
        db.execute(stmt)
        written += 1
    return written


def run_snapshot_sweep(window_days: int = SNAPSHOT_WINDOW_DAYS) -> int:
    """One pass over every connected broker-direct account (SnapTrade → realized,
    Alpaca → marked). Per-account session + commit so one bad account can't roll
    back the rest. Returns rows written."""
    end = market_hours.now_et().date()
    start = end - timedelta(days=window_days)
    with SessionLocal() as db:
        account_ids = [
            a.id for a in db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.broker.in_((BrokerName.SNAPTRADE, BrokerName.ALPACA)),
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars()
        ]
    total = 0
    for account_id in account_ids:
        try:
            with SessionLocal() as db:
                acct = db.get(BrokerAccount, account_id)
                if acct is None:
                    continue
                total += store_account_snapshots(db, acct, start, end)
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("pnl_snapshot: account %s failed", account_id)
    log.info("pnl_snapshot: sweep wrote %d day-rows across %d accounts", total, len(account_ids))
    return total


# ── Worker scheduling ────────────────────────────────────────────────────────

_task: "asyncio.Task | None" = None


def start_pnl_snapshot_job(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Spawn the periodic snapshot sweep. Idempotent. Started alongside the
    listeners (worker only)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        pass
    if loop is None:
        log.warning("pnl_snapshot: no event loop; job not started")
        return
    _task = loop.create_task(_run_loop())
    log.info("pnl_snapshot: job started (interval=%.0fs)", SNAPSHOT_INTERVAL_S)


async def stop_pnl_snapshot_job() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def _run_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_snapshot_sweep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("pnl_snapshot: sweep failed")
        await asyncio.sleep(SNAPSHOT_INTERVAL_S)
