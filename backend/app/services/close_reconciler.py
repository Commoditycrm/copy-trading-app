"""Close reconciler — flatten what the trader EXITED but the subscriber still holds.

The copy pipeline can cancel or miss a mirrored CLOSE and never re-place it —
fast in-and-out (trader bought+sold in ~50s), duplicate trader closes (fanout
made two mirror closes and the dedup canceled ALL of them), or Alpaca's
opposing-order / wash-trade block (`code 40310000`). The subscriber is then left
holding a position the trader has already exited, with NO independent stop-loss
as a backstop. Observed on prod 2026-07-24: LVWR held ~22 min after the trader
was flat; SPXW 0DTE rode to worthless.

This worker loop is the safety net. Every tick, for each connected subscriber,
it compares the subscriber's LIVE broker positions against the followed trader's
LIVE broker positions and flattens (marketable) any contract the SUBSCRIBER
holds that the TRADER no longer does.

Safety
------
This moves real money, so it ships DRY-RUN first, exactly like
``position_reconciler``:
  - ``close_reconcile_enabled`` (default False) gates the whole loop — nothing
    runs until you turn it on.
  - ``close_reconcile_apply``   (default False) → only LOGS what it WOULD close.
    Flip to True to actually place the closes, AFTER validating the dry-run.

Three guards keep it from liquidating the wrong thing:
  1. TRADER LIVE positions are ground truth. If we can't read them (fetch error,
     or the trader has no connected account) we SKIP that trader entirely —
     never flatten on incomplete data.
  2. We only touch contracts we have a COPIED order for (a mirror order exists
     for this subscriber+contract), so a subscriber's own manual positions are
     never flattened.
  3. Regular US session only — outside it, get_positions and marketable closes
     are unreliable and we must not flatten pre/post-market.

Known caveat: on aggregator brokers the trader's fresh OPEN can lag their
get_positions() briefly; validate via the dry-run log before enabling apply.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.brokers import adapter_for
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType, Order
from app.models.settings import SubscriberSettings
from app.services import market_hours
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

_BULK_CONCURRENCY = 4
_PER_ACCOUNT_TIMEOUT_S = 60.0

# Broker-agnostic contract identity so a trader on Webull and a subscriber on
# Alpaca match on the same instrument. Stocks key on (type, symbol); options add
# expiry/strike/right. Strike normalised to Decimal so 7400.0 == 7400.0000.
_Key = tuple


def _contract_key(p) -> _Key:
    return (
        p.instrument_type,
        (p.symbol or "").upper(),
        p.option_expiry,
        Decimal(p.option_strike) if p.option_strike is not None else None,
        p.option_right,
    )


async def run_loop(shutdown_check=None) -> None:
    s = get_settings()
    log.info(
        "close_reconciler loop started (enabled=%s, apply=%s)",
        s.close_reconcile_enabled, s.close_reconcile_apply,
    )
    while not (shutdown_check and shutdown_check()):
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            log.exception("close_reconciler tick failed")
        await asyncio.sleep(get_settings().close_reconcile_interval_s)


async def _tick() -> None:
    s = get_settings()
    if not s.close_reconcile_enabled:
        return
    if not market_hours.in_regular_session():
        return  # only flatten during the regular US session

    pairs = _active_pairs()
    if not pairs:
        return

    # Trader-held sets are ground truth — compute once per trader (live). None
    # means "couldn't read" → skip every subscriber of that trader this tick.
    trader_ids = {t for _, _, t in pairs}
    held_lists = await asyncio.gather(
        *(asyncio.to_thread(_trader_held_keys, t) for t in trader_ids)
    )
    trader_held: dict[uuid.UUID, set | None] = dict(zip(trader_ids, held_lists))

    apply = s.close_reconcile_apply
    sem = asyncio.Semaphore(_BULK_CONCURRENCY)

    async def _one(sub_id, acct_id, trader_id):
        keys = trader_held.get(trader_id)
        if keys is None:
            return  # incomplete trader data → never flatten
        async with sem:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_reconcile_account, sub_id, acct_id, keys, apply),
                    timeout=_PER_ACCOUNT_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "close_reconciler: sweep failed sub=%s acct=%s", sub_id, acct_id
                )

    await asyncio.gather(*(_one(*p) for p in pairs))


def _active_pairs() -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """(subscriber_id, connected_account_id, following_trader_id) for every
    subscriber that follows a trader."""
    with SessionLocal() as db:
        rows = db.execute(
            select(SubscriberSettings.user_id, SubscriberSettings.following_trader_id)
            .where(SubscriberSettings.following_trader_id.isnot(None))
        ).all()
        out: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for sub_id, trader_id in rows:
            for acct_id in db.execute(
                select(BrokerAccount.id).where(
                    BrokerAccount.user_id == sub_id,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars():
                out.append((sub_id, acct_id, trader_id))
        return out


def _trader_held_keys(trader_id: uuid.UUID) -> set | None:
    """Live contract-key set the TRADER currently holds across their connected
    accounts. Returns None on ANY failure — including no connected account — so
    the caller skips flattening rather than acting on incomplete data (a
    disconnected trader must never trigger a mass liquidation of subscribers)."""
    try:
        with SessionLocal() as db:
            accts = db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == trader_id,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars().all()
        if not accts:
            return None
        keys: set = set()
        for acct in accts:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            for p in adapter.get_positions():
                if p.quantity and p.quantity != 0:
                    keys.add(_contract_key(p))
        return keys
    except Exception:  # noqa: BLE001
        log.exception("close_reconciler: could not read trader %s positions", trader_id)
        return None


def _sub_copied_keys(db, sub_id: uuid.UUID) -> set:
    """Contract keys the subscriber has a COPIED (mirror) order for — so we only
    ever flatten positions that came from copy trading, never their own manual
    holdings."""
    keys: set = set()
    for it, sym, exp, strike, right in db.execute(
        select(
            Order.instrument_type, Order.symbol, Order.option_expiry,
            Order.option_strike, Order.option_right,
        ).where(Order.user_id == sub_id, Order.parent_order_id.isnot(None)).distinct()
    ).all():
        keys.add((
            it, (sym or "").upper(), exp,
            Decimal(strike) if strike is not None else None, right,
        ))
    return keys


def _reconcile_account(
    sub_id: uuid.UUID, acct_id: uuid.UUID, trader_keys: set, apply: bool
) -> None:
    """Flatten (or, in dry-run, just log) the subscriber's positions that the
    trader no longer holds AND that we have a copied order for."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, acct_id)
        if acct is None or acct.connection_status != "connected":
            return
        copied = _sub_copied_keys(db, sub_id)
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            positions = adapter.get_positions()
        except Exception:  # noqa: BLE001
            log.exception("close_reconciler: could not read sub %s positions", sub_id)
            return

    def _should_flatten(pos) -> bool:
        if not pos.quantity or pos.quantity == 0:
            return False
        k = _contract_key(pos)
        return k not in trader_keys and k in copied  # trader-exited AND copied

    stuck = [p for p in positions if _should_flatten(p)]
    if not stuck:
        return

    if not apply:
        for p in stuck:
            log.warning(
                "close_reconciler[DRY-RUN] would flatten sub=%s %s %s qty=%s "
                "(trader is flat on it, copied position)",
                sub_id, p.instrument_type.value, p.broker_symbol, p.quantity,
            )
        return

    # APPLY: reuse the proven bulk-close worker (marketable LIMIT for options so
    # they flatten on Alpaca too). Same filter, so it only touches `stuck`.
    from app.api.positions import _close_account_positions_sync  # noqa: PLC0415
    res = _close_account_positions_sync(
        sub_id, acct_id, uuid.UUID(int=0), None, _should_flatten, True,
    )
    if res.get("closed"):
        log.warning(
            "close_reconciler: flattened %d trader-exited position(s) for sub=%s: %s",
            len(res["closed"]), sub_id, [c.get("symbol") for c in res["closed"]],
        )
    if res.get("failed"):
        log.warning(
            "close_reconciler: %d flatten(s) FAILED for sub=%s: %s",
            len(res["failed"]), sub_id, res["failed"][:3],
        )
