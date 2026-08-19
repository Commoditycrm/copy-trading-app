"""Periodic broker-balance refresh.

The stored broker_account balance (total_equity / cash / buying_power) was only
written on connect and on the Brokers page's explicit refresh — nothing kept it
current in the background. So the Dashboard's equity could sit days stale
(prod Srinivas 2026-08-18: dashboard showed $24.8k while Alpaca was $21.0k).

Two layers keep it fresh now:
  * this background sweep refreshes every connected account on a schedule
    (Part B), so admin/other views are current even when the user isn't looking;
  * ``api.brokers.list_my_brokers`` refreshes a stale balance inline on read
    (Part A), so the value is current the moment a user opens the Dashboard.

``refresh_account_balance`` is the single source of truth for "pull the broker's
balance snapshot onto the account row" — the API's ``_refresh_balance_into``
delegates to it too.

Runs in the worker (see start_balance_sync_job).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session  # noqa: F401 — re-exported type hint convenience

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.brokers.snaptrade import SnapTradeAdapter
from app.brokers.webull import WebullAdapter
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# How often the worker refreshes every connected account's balance.
BALANCE_SYNC_INTERVAL_S = 900.0  # 15 min


def refresh_account_balance(acct: BrokerAccount, creds: dict[str, Any]) -> bool:
    """Pull the broker's balance snapshot and write it onto ``acct`` in place.
    Returns True if the row was updated.

    Best-effort: a transient rate-limit (HTTP 429) is NOT a connection problem,
    so we keep the last good balance and don't surface a scary error (returns
    False). Any other failure is recorded to ``last_error``."""
    try:
        adapter = adapter_for(acct, creds)
        if not isinstance(adapter, (AlpacaAdapter, SnapTradeAdapter, WebullAdapter)):
            return False
        bal = adapter.get_balance_snapshot()
        acct.cash = bal["cash"]
        acct.buying_power = bal["buying_power"]
        acct.total_equity = bal["total_equity"]
        acct.currency = bal["currency"]
        acct.balance_updated_at = datetime.now(timezone.utc)
        acct.last_error = None
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg or "TOO_MANY_REQUESTS" in msg or "Too many requests" in msg:
            log.info("balance_sync: rate-limited (429) for %s — keeping cached balance", acct.id)
            return False
        acct.last_error = f"balance fetch failed: {str(exc)[:400]}"
        return False


def run_balance_sweep() -> int:
    """Refresh every connected account's balance. Per-account session + commit
    so one bad account can't roll back the rest. Returns accounts updated."""
    with SessionLocal() as db:
        account_ids = [
            a.id for a in db.execute(
                select(BrokerAccount).where(BrokerAccount.connection_status == "connected")
            ).scalars()
        ]
    updated = 0
    for account_id in account_ids:
        try:
            with SessionLocal() as db:
                acct = db.get(BrokerAccount, account_id)
                if acct is None:
                    continue
                if refresh_account_balance(acct, decrypt_json(acct.encrypted_credentials)):
                    updated += 1
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("balance_sync: account %s failed", account_id)
    log.info("balance_sync: refreshed %d/%d connected accounts", updated, len(account_ids))
    return updated


# ── Worker scheduling (mirrors pnl_snapshot) ─────────────────────────────────

_task: "asyncio.Task | None" = None


def start_balance_sync_job(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Spawn the periodic balance sweep. Idempotent. Started with the listeners
    (worker only)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        pass
    if loop is None:
        log.warning("balance_sync: no event loop; job not started")
        return
    _task = loop.create_task(_run_loop())
    log.info("balance_sync: job started (interval=%.0fs)", BALANCE_SYNC_INTERVAL_S)


async def stop_balance_sync_job() -> None:
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
            await asyncio.to_thread(run_balance_sweep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("balance_sync: sweep failed")
        await asyncio.sleep(BALANCE_SYNC_INTERVAL_S)
