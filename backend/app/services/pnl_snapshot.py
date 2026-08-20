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
from app.models.user import User, UserRole
from app.services import market_hours
from app.services.broker_pnl import realized_by_day_from_broker
from app.services.crypto import decrypt_json
from app.services.pnl import realized_pnl_by_day

log = logging.getLogger(__name__)

# Refresh this many days back each pass. Options day-trades settle same day, so a
# ~5-week window comfortably covers any late broker-feed arrivals without
# re-pulling the whole history every time.
SNAPSHOT_WINDOW_DAYS = 35
# How often the worker runs a sweep. Hourly is plenty — the calendar reads frozen
# rows, and intraday freshness for the current day comes from the live fallback.
SNAPSHOT_INTERVAL_S = 3600.0
# SnapTrade's Webull activity feed can surface a day or two behind the broker, so
# `realized_by_day_from_broker` stamps a freshly traded day as $0 (no closes seen
# yet) and that $0 masks the real trades on the calendar. Within this many days of
# the sweep end we trust our own filled-order P&L on such a gap day and freeze it
# as a durable `db_fallback_lag` row (see the conditional upsert below): once
# written, a later sweep's broker $0 can't revert it — only a real broker close
# replaces it. The window bounds which days we'll ever fill from the DB (older
# flat days stay broker-authoritative, preserving the phantom-leak guard), while
# durability means a day the feed never delivers stays correct rather than
# blinking back to $0 once it ages out.
LAG_FALLBACK_DAYS = 4


def store_account_snapshots(db: Session, acct: BrokerAccount, start: date, end: date) -> int:
    """Compute broker-direct realized P&L for [start, end] for ONE account and
    upsert per-day rows keyed by (user_id, day). Returns rows written.

    Two broker-direct sources, picked by capability:
      * SnapTrade exposes a complete trade-activity feed → REALIZED P&L, FIFO'd
        (``realized_by_day_from_broker``). SnapTrade does not expose marked
        equity, so realized is the best we can do there.
      * Alpaca exposes a marked portfolio-history series, so we freeze MARKED
        P&L (``marked_pnl_by_day`` — realized + unrealized), the exact per-day
        figure Alpaca's own app shows — FORWARD-ONLY: only today's row is
        written, so settled past days are never rewritten and historical
        calendar values are preserved. Each day locks at its END-OF-DAY marked
        as it passes through "today", so an overnight position's gain lands on
        the day it accrued and the next day shows only its incremental change.
        The calendar shows the SAME metric live for today, so a settled day
        doesn't jump when the snapshot lands.
    Brokers with neither keep using the DB calc via the calendar fallback."""
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    source_by_day: dict[date, str] = {}
    eod_today: Decimal | None = None
    if hasattr(adapter, "get_account_activities"):
        daily = realized_by_day_from_broker(adapter, start, end)
        source_by_day = {d: "broker_activities" for d in daily}
        _fill_lagging_gap_days(db, acct, end, daily, source_by_day)
        # Capture today's total unrealized so the Calendar can reconstruct
        # MARKED daily P&L for this broker (no marked-history series of its own).
        eod_today = _sum_positions_unrealized(adapter)
    elif hasattr(adapter, "marked_pnl_by_day"):
        # Alpaca: freeze MARKED P&L (realized + unrealized) from portfolio-
        # history — the exact per-day figure Alpaca's own app shows — but
        # FORWARD-ONLY. We (re)write ONLY today's row; settled past days are
        # never rewritten, so historical calendar values stay EXACTLY as they
        # already are (no backfill, no retroactive change). Each day locks at
        # its end-of-day marked as it passes through "today" — the last
        # post-close sweep captures the settled figure, and from the next day on
        # it's untouched. Days from before this shipped keep their existing
        # realized-only snapshot. The calendar shows today LIVE-marked from the
        # same portfolio-history series, so there's no after-hours jump.
        full = adapter.marked_pnl_by_day(start, end)
        daily = {end: full[end]} if end in full else {}
        source_by_day = {d: "marked" for d in daily}
    else:
        return 0
    broker = acct.broker.value if acct.broker else None
    written = 0
    for day, vals in daily.items():
        # SnapTrade yields (pnl, count); Alpaca yields (pnl, count, pct). The
        # daily-return % only exists for Alpaca (portfolio-history); NULL else.
        pnl, count = vals[0], vals[1]
        pct = vals[2] if len(vals) > 2 else None
        source = source_by_day.get(day, "broker_activities")
        set_ = dict(
            realized_pnl=Decimal(pnl), trade_count=int(count), pct=pct,
            broker_account_id=acct.id, broker=broker,
            source=source, computed_at=market_hours.now_et(),
        )
        stmt = pg_insert(DailyRealizedPnlSnapshot).values(
            user_id=acct.user_id, day=day,
            realized_pnl=Decimal(pnl), trade_count=int(count), pct=pct,
            broker_account_id=acct.id, broker=broker, source=source,
        )
        # A $0 from the broker feed is either a genuinely flat day or one the feed
        # hasn't surfaced yet. Never let it overwrite a durable db_fallback_lag row
        # (a day we filled from our own fills because the feed was silent) — only a
        # real broker close (count > 0) may replace that fallback. Every other
        # write — real broker values, Alpaca marked, or the fallback itself —
        # upserts unconditionally.
        broker_silence = source == "broker_activities" and pnl == 0 and count == 0
        if broker_silence:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_daily_realized_pnl_user_day", set_=set_,
                where=DailyRealizedPnlSnapshot.source != "db_fallback_lag",
            )
        else:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_daily_realized_pnl_user_day", set_=set_,
            )
        db.execute(stmt)
        written += 1

    # Stamp today's END-OF-DAY unrealized onto today's row (created above if it
    # had activity, inserted fresh otherwise). Past rows keep the value captured
    # when they were "today" — only a real broker close (the loop above) touches
    # their realized figure. This is the second half of the marked series the
    # Calendar reconstructs.
    if eod_today is not None:
        _upsert_today_eod(db, acct, end, eod_today)
    return written


def _sum_positions_unrealized(adapter) -> Decimal | None:
    """Total unrealized P&L across the account's open positions right now, or
    None if the positions fetch fails (the day's marked stays realized-only
    rather than freezing a wrong number). Options carry a derived unrealized;
    stocks carry the broker's open_pnl. Positions with no unrealized are
    skipped."""
    try:
        positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        log.warning("pnl_snapshot: get_positions failed for EOD unrealized capture", exc_info=True)
        return None
    total = Decimal(0)
    for p in positions:
        if getattr(p, "unrealized_pnl", None) is not None:
            total += p.unrealized_pnl
    return total


def _upsert_today_eod(db: Session, acct: BrokerAccount, day: date, eod: Decimal) -> None:
    """Write ``eod_unrealized`` onto (user, day). On conflict update ONLY that
    column (+ computed_at) so we never disturb the day's realized figure; on
    insert (no row for a flat, no-trade day) create a realized-$0 row that
    carries the capture."""
    stmt = pg_insert(DailyRealizedPnlSnapshot).values(
        user_id=acct.user_id, day=day,
        realized_pnl=Decimal(0), trade_count=0, pct=None,
        broker_account_id=acct.id, broker=(acct.broker.value if acct.broker else None),
        source="broker_activities", eod_unrealized=eod,
    ).on_conflict_do_update(
        constraint="uq_daily_realized_pnl_user_day",
        set_={"eod_unrealized": eod, "computed_at": market_hours.now_et()},
    )
    db.execute(stmt)


def _fill_lagging_gap_days(
    db: Session, acct: BrokerAccount, end: date,
    daily: dict[date, tuple], source_by_day: dict[date, str],
) -> None:
    """Backfill recent days SnapTrade's feed hasn't surfaced yet from our own
    filled orders, mutating `daily`/`source_by_day` in place.

    A day is filled from the DB only when all hold: the account belongs to a
    TRADER, it's within LAG_FALLBACK_DAYS of `end` (the feed's catch-up grace
    period), the broker feed recorded no close there (count 0 — a pure gap), and
    our own orders did realize a non-zero amount that day. Days the broker already
    reported stay broker-authoritative, so correct feed values and the phantom
    guard on older flat days are untouched; once the feed catches up, the next
    sweep overwrites the fallback with the broker's own number.

    Traders only: a trader places directly at their broker, so our filled-order
    records are trustworthy on a day the feed hasn't surfaced yet (the broker
    later confirmed such a day to the cent). A subscriber's mirror fills get
    re-recorded and can drift a close onto the wrong ET day, resurfacing the very
    phantom Fix A kills — e.g. a close the broker realized on Jul 24 mis-dated to
    Jul 25. Subscribers therefore stay broker-authoritative (no DB fallback)."""
    user = db.get(User, acct.user_id)
    if user is None or user.role != UserRole.TRADER:
        return
    win_start = end - timedelta(days=LAG_FALLBACK_DAYS - 1)
    db_daily = realized_pnl_by_day(
        db, acct.user_id, start=win_start, end=end, mirrors_only=False
    )
    for day, (dpnl, dcnt) in db_daily.items():
        if dpnl == 0 and dcnt == 0:
            continue
        _bpnl, bcnt = daily.get(day, (Decimal(0), 0))
        if bcnt == 0:  # broker feed has no close here yet → trust our DB
            daily[day] = (Decimal(dpnl), int(dcnt))
            source_by_day[day] = "db_fallback_lag"


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
