"""End-of-day safety auto-close for SUBSCRIBERS (same-day-expiry only).

The problem this solves: a trader forgets to close their 0DTE options at the end
of the US session, so every subscriber is left holding contracts that expire
worthless that evening (this actually happened — SPXW puts expiring 13 Jul stayed
open for subscribers after the trader flattened their own book).

What it does: at 15:45 ET — 15 minutes before the 16:00 close — sweep every
subscriber's connected broker accounts and market-close any OPTION position whose
expiry is TODAY. Positions expiring on a later date, and all stock positions, are
left completely untouched. It runs INDEPENDENT of the trader — a pure safety net.

Pairs with the last-15-minutes new-order lockout in ``copy_engine.fanout_async``:
once the sweep starts flattening at 15:45, we also stop mirroring fresh
same-day-expiry orders, so nothing re-opens behind the sweep.

Runs as a background asyncio loop in the WORKER process only (see app/main.py),
fires ONCE per trading day (re-fires only if the worker restarts inside the
window, which is safe — a second sweep just finds nothing left to close).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType
from app.models.settings import SubscriberSettings
from app.services import market_hours

log = logging.getLogger(__name__)

# The loop wakes this often; the actual sweep still fires only once per day.
_CHECK_INTERVAL_S = 30
# Match the trader-initiated bulk-exit's concurrency/timeout so we don't burst
# SnapTrade into 429s.
_BULK_CONCURRENCY = 4
_PER_ACCOUNT_TIMEOUT_S = 60.0

# ET date we last swept, so the sweep runs ONCE even though the loop ticks every
# 30s across the whole 15-minute window. Process-local — a worker restart inside
# the window resets it, which just re-runs the (idempotent) sweep.
_last_swept: date | None = None


async def run_loop(shutdown_check=None) -> None:
    """Background loop. Every ``_CHECK_INTERVAL_S`` seconds, if we're inside the
    15:45–16:00 ET window and haven't swept today, run the same-day-expiry close.

    ``shutdown_check`` is a zero-arg callable returning True when the process is
    shutting down (we reuse main.py's shared threading.Event.is_set)."""
    log.info("eod_autoclose loop started (enabled=%s)", get_settings().eod_autoclose_enabled)
    while not (shutdown_check and shutdown_check()):
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            log.exception("eod_autoclose tick failed")
        await asyncio.sleep(_CHECK_INTERVAL_S)


async def _tick() -> None:
    global _last_swept
    if not get_settings().eod_autoclose_enabled:
        return
    now = market_hours.now_et()
    if not market_hours.in_eod_close_window(now):
        return
    today = now.date()
    if _last_swept == today:
        return  # already swept in this window today
    _last_swept = today
    log.warning(
        "eod_autoclose: entering close window at %s ET — flattening subscriber "
        "same-day-expiry (%s) option positions",
        now.isoformat(), today,
    )
    await _sweep_same_day_expiry(today)


async def _sweep_same_day_expiry(today: date) -> None:
    """Close every subscriber's same-day-expiry option positions, concurrently
    across accounts (bounded), reusing the trader bulk-exit's per-account
    worker with a filter that keeps only 0DTE options."""
    pairs = _subscriber_account_pairs()
    if not pairs:
        log.info("eod_autoclose: no connected subscriber accounts to sweep")
        return

    # Lazy import avoids an api<->service import cycle at module load
    # (app.api.positions imports services; this service is imported by main).
    from app.api.positions import _close_account_positions_sync  # noqa: PLC0415

    def _is_same_day_option(pos) -> bool:
        return (
            pos.instrument_type == InstrumentType.OPTION
            and market_hours.is_same_day_expiry(pos.option_expiry)
        )

    sem = asyncio.Semaphore(_BULK_CONCURRENCY)
    loop = asyncio.get_running_loop()

    async def _one(sub_id: uuid.UUID, acct_id: uuid.UUID, trader_id: uuid.UUID) -> dict:
        async with sem:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        None, _close_account_positions_sync,
                        # position_filter → only 0DTE options;
                        # option_marketable_limit=True → fills on Alpaca too.
                        sub_id, acct_id, trader_id, None, _is_same_day_option, True,
                    ),
                    timeout=_PER_ACCOUNT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning("eod_autoclose: timeout on sub=%s acct=%s", sub_id, acct_id)
                return {"closed": [], "failed": [{"reason": "timeout"}]}
            except Exception:  # noqa: BLE001
                log.exception("eod_autoclose: worker crashed for sub=%s acct=%s", sub_id, acct_id)
                return {"closed": [], "failed": [{"reason": "crashed"}]}

    results = await asyncio.gather(*(_one(s, a, t) for s, a, t in pairs))
    closed = sum(len(r.get("closed", [])) for r in results)
    failed = sum(len(r.get("failed", [])) for r in results)
    log.warning(
        "eod_autoclose: sweep done — closed=%d failed=%d across %d account(s)",
        closed, failed, len(pairs),
    )


def _subscriber_account_pairs() -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """(subscriber_user_id, broker_account_id, following_trader_id) for every
    CONNECTED account of every user that follows a trader. A subscriber is any
    user with SubscriberSettings.following_trader_id set — copy_enabled state is
    intentionally ignored: even a paused subscriber must be flattened out of
    expiring contracts they already hold."""
    with SessionLocal() as db:
        rows = db.execute(
            select(SubscriberSettings.user_id, SubscriberSettings.following_trader_id)
            .where(SubscriberSettings.following_trader_id.isnot(None))
        ).all()
        pairs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for sub_id, trader_id in rows:
            acct_ids = db.execute(
                select(BrokerAccount.id).where(
                    BrokerAccount.user_id == sub_id,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars().all()
            for acct_id in acct_ids:
                pairs.append((sub_id, acct_id, trader_id))
        return pairs
