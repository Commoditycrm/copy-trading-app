"""Raise a resting trailing stop within a second or two of a new high.

A trailing stop on a Discord-ladder position (Positions → Trl.Stop, or a trim
whose stop trails) is a real STOP order resting at the broker, raised as the
price makes new highs (discord_position_guard.ratchet_stop). That used to happen
only inside the P&L poller's pass over the account — every 15s on Webull — and
moving the order took a SECOND pass (cancel, then place next time), so a new high
could take ~30s to reach the broker. On a 0DTE option that is a long time.

This loop runs every second and needs no broker read to decide:

  * the price is Alpaca's live quote from the data account's stream, already in
    Redis (market_data_stream) — never a REST call, never the broker;
  * a new high raises the guard's stop level (same ratchet_stop the poller uses:
    only up, and only by at least 2% / $0.02);
  * the resting stop is then MODIFIED IN PLACE (BrokerAdapter.replace_order,
    Webull and Alpaca) — one call, no gap with nothing at the broker. At most
    once every few seconds per position. Brokers without in-place replace are
    left to the poller's cancel/re-place, as before;
  * a price through the stop with nothing resting wakes the account's poller
    pass now (poll_now) instead of at its next 15s turn.

Shares a per-user lock with the poller's Discord pass (user_lock), so the two
never write the same guard at once — the poller's stale copy could otherwise
lower a stop this loop had just raised. This loop never waits for the lock: if
the poller is busy with the user, it skips them for a second.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import select

from app.database import SessionLocal

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
# The fewest seconds between two broker modifies of the same stop. The ratchet's
# own minimum step keeps moves rare; this bounds a fast-rising contract.
MIN_MODIFY_GAP_S = 3.0
# A cached price older than this is not "live" — wait for the stream.
PRICE_MAX_AGE_S = 5.0

_locks: dict[uuid.UUID, threading.RLock] = {}
_locks_guard = threading.Lock()
_last_modify: dict[uuid.UUID, float] = {}


def user_lock(user_id) -> threading.RLock:
    """The lock both this loop and the poller's Discord pass hold while they
    read and write a user's ladder guards (same process: the worker)."""
    with _locks_guard:
        return _locks.setdefault(user_id, threading.RLock())


def _live_price(guard) -> Decimal | None:
    """Alpaca's live price for the guard's contract, from the stream cache only."""
    from app.services import live_marks, market_data_stream  # noqa: PLC0415

    key = live_marks.quote_key(guard.symbol, guard.option_strike, guard.option_right,
                               guard.option_expiry)
    if not key:
        return None
    try:
        px = market_data_stream.get_live_price(key, max_age_s=PRICE_MAX_AGE_S)
    except Exception:  # noqa: BLE001
        return None
    return Decimal(str(px)) if px is not None and px > 0 else None


def _px(v) -> str:
    """A price for the log: 1.3000 -> 1.3, never scientific."""
    return f"{Decimal(str(v)).normalize():f}" if v is not None else "?"


def _working(order) -> bool:
    from app.models.order import OrderStatus  # noqa: PLC0415

    return order is not None and order.status in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    )


def _modify_stop(db, order, new_stop: Decimal) -> str:
    """Move the resting stop to ``new_stop`` in place. Returns an outcome."""
    from app.api.trades import broker_request_for  # noqa: PLC0415
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services import order_intent  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    acct = db.get(BrokerAccount, order.broker_account_id)
    if acct is None or acct.connection_status != "connected":
        return "no broker"
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    if not getattr(adapter, "supports_replace", False):
        return "no in-place replace (the poller re-places it)"
    # Alpaca's replace opens a NEW order, and a client_order_id may be held by
    # only one active order, so the replacement needs its own — marked ours
    # first, or the order stream imports it as an outside trade (see
    # discord_reprice). Webull replaces in place and keeps its id.
    new_coid = uuid.uuid4()
    order_intent.mark_app_originated(new_coid)
    req = replace(broker_request_for(order, False), stop_price=new_stop,
                  client_order_id=str(new_coid))
    result = adapter.replace_order(order.broker_order_id, req)
    if getattr(result, "broker_order_id", None):
        order.broker_order_id = result.broker_order_id
    order.stop_price = new_stop
    return "moved"


def ratchet_one(guard_id) -> str | None:
    """One guard: raise its trailing stop on a new high and move the resting
    order to it. Caller holds the user's lock. Returns an outcome, or None."""
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415
    from app.services import position_events  # noqa: PLC0415

    with SessionLocal() as db:
        g = db.get(DiscordPositionGuard, guard_id)
        if g is None or g.closed_at is not None or g.stop_trail_pct is None or g.stop_price is None:
            return None
        # What Webull refuses here goes into the position's summary.
        with position_events.about(g.user_id, g.symbol, g.option_strike, g.option_right, g.option_expiry):
            return _ratchet(db, g)


def _ratchet(db, g) -> str | None:
    from app.models.order import Order  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415
    from app.services import market_hours, position_events  # noqa: PLC0415
    from app.services.position_events import because  # noqa: PLC0415

    price = _live_price(g)
    if price is None:
        return None
    before = Decimal(str(g.stop_price))
    peak_now = max(Decimal(str(g.stop_peak)), price) if g.stop_peak is not None else price
    with because(f"new high {price} — " + guards.trail_explain(g.stop_trail_pct, peak_now, g.entry_price)):
        raised = guards.ratchet_stop(g, price)
        db.flush()
    outcome = f"{g.symbol} trailing stop {_px(before)} -> {_px(g.stop_price)}" if raised else None

    resting = db.get(Order, g.stop_order_id) if g.stop_order_id else None
    if resting is None and getattr(g, "tp_stop_order_id", None) is None:
        # Nothing at the broker for this level (between a cancel and its
        # re-place, or refused): the poller enforces it — now, not in 15s.
        if price <= Decimal(str(g.stop_price)):
            db.commit()
            _wake(db, g.user_id)
            return "through the stop with nothing resting — poller woken"
        db.commit()
        return outcome

    if (_working(resting) and resting.stop_price is not None
            and Decimal(str(resting.stop_price)) < Decimal(str(g.stop_price))
            and market_hours.in_regular_session()):
        now = time.monotonic()
        if now - _last_modify.get(g.id, 0.0) >= MIN_MODIFY_GAP_S:
            _last_modify[g.id] = now
            old = resting.stop_price
            try:
                moved = _modify_stop(db, resting, Decimal(str(g.stop_price)))
            except Exception as exc:  # noqa: BLE001
                # The original stays working at its old level (an in-place
                # replace is atomic); the poller re-places it on its pass.
                resting.stop_price = old
                log.warning("trail ratchet: could not move %s's stop to %s: %s",
                            g.symbol, g.stop_price, str(exc)[:200])
                position_events.record_issue(
                    "refused",
                    f"Webull refused moving the trailing stop {_px(old)} → {_px(g.stop_price)}: "
                    f"{position_events.short_reason(str(exc))}. It stays at {_px(old)}; "
                    "the ladder's next pass tries again.", guard=g)
                moved = "modify failed"
            outcome = (f"{outcome or g.symbol + ' stop behind its level'}; broker stop "
                       f"{_px(old)} -> {_px(resting.stop_price)} ({moved})")
    db.commit()
    return outcome


def _wake(db, user_id) -> None:
    """Make this user's accounts due on the poller's very next tick."""
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services import pnl_poller  # noqa: PLC0415

    for acct_id in db.execute(
        select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars():
        pnl_poller.poll_now(acct_id)


def tick() -> int:
    """One pass over every live trailing stop. Returns how many were acted on."""
    from app.models.discord_position_guard import DiscordPositionGuard as G  # noqa: PLC0415

    with SessionLocal() as db:
        rows = db.execute(
            select(G.id, G.user_id).where(
                G.closed_at.is_(None),
                G.stop_trail_pct.isnot(None),
                G.stop_price.isnot(None),
            )
        ).all()
    acted = 0
    for guard_id, user_id in rows:
        lock = user_lock(user_id)
        if not lock.acquire(blocking=False):
            continue                      # the poller has this user right now
        try:
            out = ratchet_one(guard_id)
        except Exception:  # noqa: BLE001
            log.exception("trail ratchet: failed on guard %s", guard_id)
            out = None
        finally:
            lock.release()
        if out:
            acted += 1
            log.info("trail ratchet: %s", out)
    return acted


def poll_loop(shutdown_check=None) -> None:
    log.info("trail_ratchet: starting (interval=%ss)", POLL_INTERVAL_S)
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("trail_ratchet: shutdown requested, exiting")
            return
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("trail_ratchet: tick failed")
        time.sleep(POLL_INTERVAL_S)


__all__ = ["poll_loop", "tick", "ratchet_one", "user_lock"]
