"""Daily P&L limit poller — broker-agnostic.

Every 5 seconds, for every subscriber with a connected Alpaca-direct or
SnapTrade-routed broker account:

  1. Call ``adapter.get_pnl_snapshot()`` to get today's P&L, current
     equity, and the day-start account balance. For Alpaca that's one
     ``GET /v2/account`` (equity - last_equity); for SnapTrade it's
     balance + account-details (day-start is broker-dependent and may
     come back None).
  2. If the subscriber has a ``daily_loss_limit`` or ``daily_profit_limit``
     set AND today's P&L breaches it, flip ``copy_enabled = False`` and
     stamp ``pnl_auto_paused_at``. Same audit + cache-invalidation + SSE
     event shape the in-fanout enforcement uses.
  3. If ``max_account_pct_per_day`` is set AND today's filled trade
     notional (from our own fills table) crosses
     ``beginning_day_balance * pct/100``, same pause. Skipped silently
     when the broker doesn't expose a day-start.
  4. Auto-resume any pause whose ``pnl_auto_paused_at`` is from a prior
     UTC day. Runs here (not just in copy_engine on fanout) so a
     subscriber paused yesterday comes back online at 00:00 UTC even if
     no trader places an order that day.
  5. Emit a ``pnl.tick`` SSE event so the Settings page's P&L Limit and
     Risk Limits panels update live.

Cadence
-------
5s per tick at the user's request. Per-account work runs concurrently
via ``asyncio.gather(*to_thread(...))`` so wall-clock per tick is the
slowest single broker call (~500ms), not the sum. At 5s polling, each
Alpaca account costs 12 req/min against its own 200/min budget; each
SnapTrade account costs 24 req/min (balance + details) against the
shared platform quota — comfortably under SnapTrade's per-endpoint
limits at any realistic platform size.

Why a separate task (not piggyback on copy_engine)
--------------------------------------------------
copy_engine's check only runs when a trader fanouts. If the trader is
quiet for hours but the subscriber's positions move against them, the
limit goes un-policed. This poller fills that gap.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.settings import SubscriberSettings
from app.services import audit, cache, events
from app.services.crypto import decrypt_json
from app.services.pnl import today_filled_notional

log = logging.getLogger(__name__)


# 5s cadence per user request. The per-account work runs in parallel via
# ``asyncio.gather`` so the wall-clock per tick is dominated by the
# slowest single broker call (~500ms typical), not the sum.
POLL_INTERVAL_S = 5.0

# Brokers the poller knows how to fetch from. Adding a broker is one of:
# (a) the adapter implements ``get_pnl_snapshot()``, and (b) the broker
# is listed here. Everything else is silently skipped.
_SUPPORTED_BROKERS = (BrokerName.ALPACA, BrokerName.SNAPTRADE)


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
    """Outer loop: every POLL_INTERVAL_S, load the active broker accounts
    and fan ``_enforce_one`` out over the threadpool concurrently. With
    the 5s cadence, sequential per-account work would miss ticks once we
    cross ~10 connected accounts (each broker call is 100-500ms); gather
    makes per-tick wall-clock the slowest single call, not the sum."""
    while True:
        try:
            accts = await asyncio.to_thread(_load_active_accounts)
            if accts:
                await asyncio.gather(
                    *(asyncio.to_thread(_enforce_one_safe, acct) for acct in accts),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            log.info("pnl_poller: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("pnl_poller: tick failed")
        await asyncio.sleep(POLL_INTERVAL_S)


def _load_active_accounts() -> list[BrokerAccount]:
    """Snapshot every connected broker_account whose broker has a
    ``get_pnl_snapshot`` implementation. Detaches the rows from the
    session so ``_enforce_one`` can read scalar attributes after the
    session closes without DetachedInstanceError."""
    with SessionLocal() as db:
        accts = list(db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker.in_(_SUPPORTED_BROKERS),
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())
        for a in accts:
            db.expunge(a)
        return accts


def _enforce_one_safe(acct: BrokerAccount) -> None:
    """Crash isolation wrapper — one subscriber's failure (broker 500,
    DB lock, etc.) must not abort the rest of the tick's gather."""
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

    # Fetch the broker P&L snapshot FIRST — with NO DB session/connection held.
    # SnapTrade rate-limits (429) and this call can block for seconds; doing it
    # inside `with SessionLocal()` pinned a pool connection in "idle in
    # transaction" for the whole call. With every connected account polled
    # concurrently every tick, that exhausted the pool (15/15) and stalled all
    # DB-backed APIs. _fetch_pnl_snapshot only needs `acct` (no DB), so do it
    # up front and only open the session for the fast enforcement writes.
    state = _fetch_pnl_snapshot(acct)

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

        # today's P&L snapshot (todays_pl / equity / beginning_day_balance) was
        # fetched ABOVE, before this session opened, so we never hold a pool
        # connection across the slow broker call. beginning_day_balance may be
        # None for SnapTrade brokers that don't expose a day-start figure; the
        # pct kill switch is silently skipped for those subscribers.
        if state is None:
            # Broker call failed — commit any auto-resume we did, skip
            # the rest of this tick, try again next time.
            if pending_events:
                db.commit()
            _flush(s.user_id, pending_events)
            if invalidate_trader_id:
                _safe_invalidate(invalidate_trader_id)
            return
        todays_pl = state["todays_pl"]
        equity = state["equity"]
        beginning_day_balance: Decimal | None = state.get("beginning_day_balance")

        # ── Pct-of-day-start-balance TRADING-VALUE cap ───────────────────
        # Tracks today's cumulative filled trade notional (capital
        # deployed, not P&L). When today's trading USD crosses
        # beginning_day_balance * pct/100, copy is paused. Using the
        # day-start balance means the dollar threshold is FIXED for the
        # trading day; if we used live equity, the threshold would drift
        # up on gains and down on losses, which would be confusing.
        todays_trading_value = today_filled_notional(db, s.user_id)

        pct_limit_dollars: Decimal | None = None
        if (
            s.max_account_pct_per_day is not None
            and beginning_day_balance is not None
            and beginning_day_balance > 0
        ):
            pct_limit_dollars = beginning_day_balance * s.max_account_pct_per_day / Decimal(100)

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
                    "broker":                  acct.broker.value,
                    "todays_pl":               str(todays_pl),
                    "todays_trading_value":    str(todays_trading_value),
                    "equity":                  str(equity),
                    "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
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
                "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
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
            "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
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


def _fetch_pnl_snapshot(acct: BrokerAccount) -> dict[str, Any] | None:
    """Broker-agnostic fetch. Decrypts creds, builds the right adapter
    via ``adapter_for``, calls ``get_pnl_snapshot``. Returns the dict
    shape ``{"todays_pl", "equity", "beginning_day_balance"}`` or None
    on any failure (caller skips the tick). ``beginning_day_balance``
    inside the dict may itself be None for SnapTrade brokers that don't
    expose a day-start — the pct kill switch is skipped in that case
    while the loss/profit limits and live tile still work."""
    try:
        creds = decrypt_json(acct.encrypted_credentials)
    except Exception:  # noqa: BLE001
        log.exception("pnl_poller: decrypt failed for account %s", acct.id)
        return None
    try:
        adapter = adapter_for(acct, creds)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pnl_poller: adapter_for failed for account %s: %s", acct.id, exc,
        )
        return None
    try:
        return adapter.get_pnl_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pnl_poller: %s get_pnl_snapshot failed for account %s: %s",
            adapter.name, acct.id, exc,
        )
        return None


__all__ = ["bind_loop", "start", "stop", "POLL_INTERVAL_S"]
