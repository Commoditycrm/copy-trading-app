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

from sqlalchemy import exists, select

from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType
from app.models.settings import SubscriberSettings
from app.models.subscriber_follow import SubscriberFollow
from app.services import market_hours

log = logging.getLogger(__name__)

# The loop wakes this often; each subscriber is still swept only once per day.
# Kept short so a 1–2 minute per-subscriber window still gets several ticks.
_CHECK_INTERVAL_S = 15
# Match the trader-initiated bulk-exit's concurrency/timeout so we don't burst
# SnapTrade into 429s.
_BULK_CONCURRENCY = 4
_PER_ACCOUNT_TIMEOUT_S = 60.0

# Per-subscriber ET date last swept, so each subscriber is flattened ONCE even
# though the loop ticks repeatedly across their window. Process-local — a worker
# restart inside a window just re-runs the (idempotent) sweep for that sub.
_last_swept: dict[uuid.UUID, date] = {}


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
    if not get_settings().eod_autoclose_enabled:
        return  # global master kill-switch (per-subscriber opt-in layers under it)
    now = market_hours.now_et()
    if not market_hours.is_trading_weekday(now):
        return
    today = now.date()

    # Per-subscriber: each opted-in subscriber has their OWN window
    # (eod_autoclose_minutes before 16:00 ET). Collect the accounts whose
    # subscriber's window is open now and who haven't been swept yet today.
    due = _due_pairs(now, today)
    if not due:
        return
    swept_subs = {sub_id for sub_id, _, _ in due}
    for sub_id in swept_subs:
        _last_swept[sub_id] = today
    log.warning(
        "eod_autoclose: window open for %d subscriber(s) at %s ET — flattening "
        "their same-day-expiry (%s) option positions",
        len(swept_subs), now.isoformat(), today,
    )
    await _sweep_pairs(due, today)


async def _sweep_pairs(
    pairs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]], today: date
) -> None:
    """Close the given (subscriber, account, trader) triples' same-day-expiry
    option positions, concurrently across accounts (bounded), reusing the trader
    bulk-exit's per-account worker with a filter that keeps only 0DTE options."""
    if not pairs:
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


def _due_pairs(
    now: "datetime", today: date
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """(subscriber_user_id, broker_account_id, following_trader_id) for every
    CONNECTED account of every OPTED-IN subscriber whose personal EOD window is
    open right now and who hasn't been swept yet today.

    Qualifies when: follows a trader, eod_autoclose_enabled is True, and ``now``
    is within their [close − eod_autoclose_minutes, close) window. copy_enabled/
    paused state is intentionally ignored — even a paused subscriber must be
    flattened out of expiring contracts they already hold.

    Multi-trader: the sweep is ACCOUNT-centric (it flattens every 0DTE option the
    account holds, regardless of which trader it was mirrored from), so we emit
    exactly ONE pair per (subscriber, account) — NOT one per followed trader, which
    would sweep the same account concurrently N times. Qualification is an EXISTS
    on ``subscriber_follows`` so it doesn't rely on the primary ``following_trader_id``
    invariant. The trader_id slot carries the primary follow purely for audit and
    is unused by the close worker."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                SubscriberSettings.user_id,
                SubscriberSettings.following_trader_id,
                SubscriberSettings.eod_autoclose_minutes,
            ).where(
                exists().where(SubscriberFollow.subscriber_id == SubscriberSettings.user_id),
                SubscriberSettings.eod_autoclose_enabled.is_(True),
            )
        ).all()
        pairs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for sub_id, trader_id, minutes in rows:
            if _last_swept.get(sub_id) == today:
                continue  # already flattened this subscriber today
            if not market_hours.in_eod_close_window(now, minutes=minutes):
                continue  # not inside THIS subscriber's window yet
            acct_ids = db.execute(
                select(BrokerAccount.id).where(
                    BrokerAccount.user_id == sub_id,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars().all()
            for acct_id in acct_ids:
                pairs.append((sub_id, acct_id, trader_id))
        return pairs
