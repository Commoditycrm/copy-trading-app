"""Daily P&L limit poller — Alpaca-direct.

Every 60 seconds, for every subscriber with a connected Alpaca account:

  1. Hit Alpaca's ``GET /v2/account`` and compute today's P&L as
     ``equity - last_equity`` (matches the number Alpaca's own dashboard
     shows under "Today's P/L"). No FIFO walk over our local fills —
     this is the broker's own bookkeeping, taken at face value.
  2. If the subscriber has a ``daily_loss_limit`` or
     ``daily_profit_limit`` set AND today's P&L breaches it, flip
     ``copy_enabled = False`` and stamp ``pnl_auto_paused_at = now``.
     Same audit + cache-invalidation + SSE event shape the in-fanout
     enforcement uses, so subscribers get one consistent experience.
  3. Auto-resume any pause whose ``pnl_auto_paused_at`` is from a prior
     UTC day. Runs here (not just in copy_engine on fanout) so a
     subscriber paused yesterday comes back online at 00:00 UTC even if
     no trader places an order that day.
  4. Emit a ``pnl.tick`` SSE event with the latest number so the
     Settings page's P&L Limit panel updates live.

Why a separate task (not piggyback on copy_engine)
--------------------------------------------------
copy_engine's check only runs when a trader fanouts. If the trader is
quiet for hours but the subscriber's positions move against them, the
limit goes un-policed. This poller fills that gap.

Why one global task (not per-account)
-------------------------------------
The per-account work is one HTTP call to Alpaca; spinning a task per
account adds bookkeeping without parallelism gains worth caring about
at the platform's current scale. If we ever cross ~500 active Alpaca
subscribers, switch to a worker pool.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.alpaca import AlpacaAdapter
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.settings import SubscriberSettings
from app.services import audit, cache, events
from app.services.crypto import decrypt_json
from app.services.pnl import today_filled_notional

log = logging.getLogger(__name__)


POLL_INTERVAL_S = 60.0


_task: asyncio.Task | None = None
_main_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def start() -> None:
    """Spawn the single polling task. Idempotent — calling twice is a no-op."""
    global _task
    if _task and not _task.done():
        return
    if _main_loop is None:
        log.warning("pnl_poller: no main loop bound; start is a no-op")
        return
    _task = _main_loop.create_task(_run())
    log.info("pnl_poller: started (interval=%.0fs)", POLL_INTERVAL_S)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def _run() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            log.info("pnl_poller: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("pnl_poller: tick failed")
        await asyncio.sleep(POLL_INTERVAL_S)


def _tick() -> None:
    """One full sweep: every connected Alpaca account → fetch + enforce.

    No top-level commit — ``_enforce_one`` opens its own session per
    subscriber so each subscriber's mutations are durable BEFORE its SSE
    events go out. That ordering prevents a UI race where the frontend's
    refetch (triggered by ``copy.auto_paused``) reads an uncommitted
    transaction and gets stale ``copy_enabled=true``."""
    with SessionLocal() as db:
        accts = list(db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker == BrokerName.ALPACA,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())

    for acct in accts:
        try:
            _enforce_one(acct)
        except Exception:  # noqa: BLE001
            log.exception(
                "pnl_poller: enforce failed for account %s (user %s)",
                acct.id, acct.user_id,
            )


def _enforce_one(acct: BrokerAccount) -> None:
    """One subscriber's tick. Opens its own session so the commit lands
    BEFORE the SSE events go out — the frontend's refetch on
    ``copy.auto_paused`` would otherwise race the poller's transaction
    and read the pre-pause state, leaving the toggle visually stuck on
    until the next manual refresh."""
    pending_events: list[dict[str, Any]] = []

    with SessionLocal() as db:
        s = db.get(SubscriberSettings, acct.user_id)
        if s is None:
            # The Alpaca account belongs to an admin or trader (no
            # SubscriberSettings row) — nothing to enforce here.
            return

        now_utc = datetime.now(timezone.utc)
        invalidate_trader_id: uuid.UUID | None = None

        # ── Auto-resume next-UTC-day ──────────────────────────────────────
        paused_at = s.pnl_auto_paused_at
        if paused_at is not None:
            if paused_at.tzinfo is None:
                paused_at = paused_at.replace(tzinfo=timezone.utc)
            if paused_at.astimezone(timezone.utc).date() < now_utc.date():
                s.copy_enabled = True
                s.pnl_auto_paused_at = None
                audit.record(
                    db, actor_user_id=s.user_id,
                    action="copy.auto_resumed_next_day",
                    entity_type="subscriber_settings", entity_id=s.user_id,
                    metadata={"source": "pnl_poller"},
                )
                if s.following_trader_id:
                    invalidate_trader_id = s.following_trader_id
                pending_events.append({
                    "type": "copy.auto_resumed", "reason": "new_day",
                })

        # ── Fetch today's P&L + current equity from Alpaca ───────────────
        state = _fetch_alpaca_state(acct)
        if state is None:
            # Broker call failed — commit any auto-resume we did, skip
            # the rest of this tick, try again next time.
            if pending_events:
                db.commit()
            _flush(s.user_id, pending_events)
            if invalidate_trader_id:
                _safe_invalidate(invalidate_trader_id)
            return
        todays_pl, equity = state

        # ── Pct-of-equity TRADING-VALUE cap ──────────────────────────────
        # Tracks today's cumulative filled trade notional (capital
        # deployed, not P&L). When today's trading USD crosses
        # equity*pct/100, copy is paused. Computed from our own fills
        # table (DB-derived), independent of the equity-delta P&L number
        # used by the loss/profit limits.
        todays_trading_value = today_filled_notional(db, s.user_id)

        pct_limit_dollars: Decimal | None = None
        if s.max_account_pct_per_day is not None and equity > 0:
            pct_limit_dollars = equity * s.max_account_pct_per_day / Decimal(100)

        hit_loss = (
            s.daily_loss_limit is not None and todays_pl <= -s.daily_loss_limit
        )
        hit_profit = (
            s.daily_profit_limit is not None and todays_pl >= s.daily_profit_limit
        )
        hit_pct = (
            pct_limit_dollars is not None and todays_trading_value >= pct_limit_dollars
        )

        if s.copy_enabled and (hit_loss or hit_profit or hit_pct):
            if hit_loss:
                reason = "daily_loss_limit"
            elif hit_profit:
                reason = "daily_profit_limit"
            else:
                reason = "max_account_pct_per_day"
            s.copy_enabled = False
            s.pnl_auto_paused_at = now_utc
            audit.record(
                db, actor_user_id=s.user_id,
                action=f"copy.auto_paused_{reason}",
                entity_type="subscriber_settings", entity_id=s.user_id,
                metadata={
                    "source":                  "pnl_poller",
                    "todays_pl":               str(todays_pl),
                    "todays_trading_value":    str(todays_trading_value),
                    "equity":                  str(equity),
                    "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
                    "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
                    "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
                    "pct_limit_dollars":       str(pct_limit_dollars) if pct_limit_dollars is not None else None,
                },
            )
            if s.following_trader_id:
                invalidate_trader_id = s.following_trader_id
            # Reuses the existing `copy.auto_paused` event shape that the
            # Settings page already listens to (toasts the pause notice).
            pending_events.append({
                "type": "copy.auto_paused",
                "reason": reason,
                "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
                "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
                "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
                "pct_limit_dollars":       str(pct_limit_dollars) if pct_limit_dollars is not None else None,
                "todays_realized_pnl":     str(todays_pl),
                "todays_trading_value":    str(todays_trading_value),
            })

        # ── Commit BEFORE any events go out ───────────────────────────────
        # Commit only when there were actual mutations — pnl.tick alone
        # is a pure read and shouldn't bump audit timestamps. We snapshot
        # the values needed for the tick payload BEFORE the commit so the
        # publish below isn't reading post-commit ORM state.
        tick_payload = {
            "type":                    "pnl.tick",
            "todays_realized_pnl":     str(todays_pl),
            "todays_trading_value":    str(todays_trading_value),
            "equity":                  str(equity),
            "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
            "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
            "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
            "max_per_contract":        str(s.max_per_contract) if s.max_per_contract else None,
            "copy_enabled":            s.copy_enabled,
        }
        user_id_snapshot = s.user_id
        if pending_events:
            db.commit()

    # ── Outside the session: publish events + invalidate caches ──────────
    # Doing this AFTER the session closes guarantees the transaction is
    # durable — the frontend's refetch on `copy.auto_paused` will read
    # the committed row and the UI toggle reflects the pause instantly.
    if invalidate_trader_id:
        _safe_invalidate(invalidate_trader_id)
    _flush(user_id_snapshot, pending_events)
    events.publish(user_id_snapshot, tick_payload)


def _safe_invalidate(trader_id: uuid.UUID) -> None:
    """Bust the trader's subscriber cache; swallow Redis hiccups so a
    transient cache outage never blocks the per-subscriber loop."""
    try:
        cache.invalidate_subscribers_for_trader(trader_id)
    except Exception:  # noqa: BLE001
        log.warning("pnl_poller: cache invalidate failed for trader %s", trader_id)


def _flush(user_id: uuid.UUID, pending: list[dict[str, Any]]) -> None:
    for evt in pending:
        events.publish(user_id, evt)


def _fetch_alpaca_state(acct: BrokerAccount) -> tuple[Decimal, Decimal] | None:
    """Pull ``equity`` and ``last_equity`` from Alpaca, return
    ``(todays_pl, equity)``. Today's P&L is the dashboard-style
    ``equity - last_equity``; equity itself is needed separately so the
    pct-of-equity limit can derive a current-dollar threshold each tick.
    None on any failure — the caller skips this tick rather than killing
    the loop."""
    try:
        creds = decrypt_json(acct.encrypted_credentials)
    except Exception:  # noqa: BLE001
        log.exception("pnl_poller: decrypt failed for account %s", acct.id)
        return None
    try:
        adapter = AlpacaAdapter(creds)
        a = adapter._c().get_account()  # noqa: SLF001 — single-call shortcut, no need for a public wrapper
        equity = Decimal(str(a.equity))
        last_equity = Decimal(str(a.last_equity))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pnl_poller: alpaca get_account failed for %s: %s",
            acct.id, exc,
        )
        return None
    return (equity - last_equity), equity


__all__ = ["bind_loop", "start", "stop", "POLL_INTERVAL_S"]
