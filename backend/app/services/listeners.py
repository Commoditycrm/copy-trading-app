"""Broker-agnostic listener dispatcher.

Callers (FastAPI lifespan, /api/brokers connect/disconnect, etc.) talk to
this module instead of importing ``trade_listener`` / ``ibkr_listener`` /
``snaptrade_listener`` directly. Routing is by ``BrokerName``.

Direct Webull integration has been removed — users connect Webull through
SnapTrade (which lands as ``BrokerName.SNAPTRADE`` rows handled by
``snaptrade_listener``).

Status reads are unified: ``listener_state.get_status`` returns the live
state regardless of which broker actually drives it. All backends share
the same status dict.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.services import (
    ibkr_listener,
    listener_state,
    snaptrade_listener,
    trade_listener,
)

log = logging.getLogger(__name__)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Forward to every backend. Cheap — just stores a reference."""
    trade_listener.bind_loop(loop)
    snaptrade_listener.bind_loop(loop)
    ibkr_listener.bind_loop(loop)


async def start_all_listeners() -> None:
    """Spawn listeners for every connected broker account on app startup."""
    await trade_listener.start_all_listeners()
    await snaptrade_listener.start_all_listeners()
    await ibkr_listener.start_all_listeners()


async def stop_all_listeners() -> None:
    """Symmetric shutdown — every backend drains its tasks."""
    await trade_listener.stop_all_listeners()
    await snaptrade_listener.stop_all_listeners()
    await ibkr_listener.stop_all_listeners()


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Route to the right backend based on the account's broker. Looks up
    the BrokerAccount row to avoid making callers pass the broker name."""
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
    if acct is None:
        log.warning(
            "listeners.start_listener: account %s not found", broker_account_id
        )
        return
    if acct.broker == BrokerName.ALPACA:
        trade_listener.start_listener(trader_user_id, broker_account_id)
    elif acct.broker == BrokerName.SNAPTRADE:
        snaptrade_listener.start_listener(trader_user_id, broker_account_id)
    elif acct.broker == BrokerName.IBKR:
        ibkr_listener.start_listener(trader_user_id, broker_account_id)
    else:
        # Includes BrokerName.WEBULL (dormant — historical rows only) and
        # BrokerName.FAKE (no live listener needed).
        log.info(
            "listeners.start_listener: no listener for broker %s",
            acct.broker.value,
        )


def stop_listener(trader_user_id: uuid.UUID) -> None:
    """Stop whichever backend is currently servicing this trader. One-
    broker-per-user means at most one will have a task — but we call
    every backend so transitions (Alpaca → SnapTrade → IBKR or any
    other permutation) are always clean.

    All calls publish a final ``disconnected`` state via
    ``listener_state.set_state``; subsequent calls are no-ops for
    status purposes (already disconnected) but safely remove stragglers."""
    trade_listener.stop_listener(trader_user_id)
    snaptrade_listener.stop_listener(trader_user_id)
    ibkr_listener.stop_listener(trader_user_id)
    # Drop the entry entirely so the SSE pill doesn't keep showing a
    # 'disconnected' state for a broker the user no longer has.
    listener_state.clear(trader_user_id)


# Backwards-compat re-export — some call sites still import get_status from
# trade_listener. listener_state is the source of truth now.
get_status = listener_state.get_status
