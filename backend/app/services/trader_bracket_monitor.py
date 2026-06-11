"""Trader-side option SL price monitor.

Background
----------
The bracket_emulator places trader TP/SL exits at the broker as LIMIT
(for TP) and STOP (for SL) orders. That works on stocks. For OPTIONS,
Alpaca's API (and SnapTrade routing to Alpaca) explicitly rejects
STOP / STOP_LIMIT order types — only MARKET and LIMIT are allowed.
The emulator now defers the option SL leg to this monitor instead
of placing a broken broker order.

What this monitor does
----------------------
Runs on the pnl_poller tick (same cadence as kill-switch / position
TP-SL enforcement). For every connected trader account, fetches open
positions, joins them to the FILLED entry order with stop_loss_price
set, and when the current option mark crosses the SL threshold:

  1. Cancels the TP leg that the bracket_emulator already placed
     (otherwise we'd have two open exits competing).
  2. Places a SELL LIMIT close at the current mark. We use LIMIT not
     MARKET because option market orders need a live quote (the
     same 40310000 "no available quote" failure mode we saw on the
     subscriber position enforcer).
  3. Persists the close, audits, publishes SSE + notification so the
     trader sees it land in real time.

Idempotency
-----------
Each call checks for an existing in-flight close order for the same
position before triggering. If a prior tick already placed the close
and the broker hasn't filled yet, the next tick won't double-trigger.

Symmetric for short positions (sold-to-open puts/calls): TP triggers
when option price drops, SL triggers when price rises. The math reads
the entry's side and flips the comparison accordingly.

Scope
-----
Options only. Stocks already work with native STOP — the emulator's
existing path handles them and this monitor short-circuits for
``InstrumentType.STOCK``.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.base import BrokerOrderRequest, BrokerPosition
from app.models.broker_account import BrokerAccount
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services import audit
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


# Statuses we treat as "still alive at the broker" — used to detect an
# in-flight close (so we don't double-trigger) and to find the TP leg
# that needs cancelling when SL fires.
_ALIVE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)

# Option ticks — same rules as bracket_emulator + position_enforcer.
_PENNY = Decimal("0.01")
_NICKEL = Decimal("0.05")
_OPTION_NICKEL_THRESHOLD = Decimal("3.00")


def _round_close_limit(price: Decimal, side: OrderSide) -> Decimal:
    """Round to the option tick the broker will accept, in the
    direction that makes the limit MORE likely to fill (don't shave
    fills in the wrong direction)."""
    mode = ROUND_DOWN if side == OrderSide.SELL else ROUND_UP
    tick = _NICKEL if price >= _OPTION_NICKEL_THRESHOLD else _PENNY
    return (price / tick).quantize(Decimal("1"), rounding=mode) * tick


def _sl_breached(pos: BrokerPosition, entry: Order, is_long: bool) -> bool:
    """Two-signal SL trigger.

    1. **Unrealized-P&L percent** (primary). Derive the SL percent from
       the entry price + stop_loss_price, then compare to live
       ``unrealized_pnl / |cost_basis|``. Matches what
       ``position_enforcer`` does for subscribers — and crucially it
       captures the bid-ask spread the moment we fill (broker's
       market_value reflects the price you could SELL at, so a long
       option bought at the ask shows immediate unrealized loss equal
       to the spread). This is why subscribers fire instantly while
       the price-only signal would wait for the option's last trade
       to actually print at the SL level.
    2. **Price vs stop_loss_price** (fallback). Triggers when the
       option's current mark crosses the SL level, even if the broker
       hasn't refreshed the position's unrealized_pnl yet. Also covers
       traders who set an SL price that doesn't correspond to a
       round percent (e.g. a manual psychological level).

    EITHER signal trips the close. Whichever fires first wins — same
    cancel-TP-then-place-limit-close path runs in both cases.
    """
    # Signal 2 — direct price comparison. Cheap and always available.
    if is_long and pos.current_price <= entry.stop_loss_price:
        return True
    if not is_long and pos.current_price >= entry.stop_loss_price:
        return True

    # Signal 1 — unrealized-P&L percent. Needs cost_basis + unrealized_pnl
    # from the broker AND a known entry reference price. If any is
    # missing we silently fall back to the price-only check above.
    if pos.unrealized_pnl is None or pos.cost_basis is None:
        return False
    basis = abs(pos.cost_basis)
    if basis == 0:
        return False
    # Reference price for the SL percent: prefer the entry's actual
    # fill price; fall back to limit/stop if for some reason fill
    # didn't write back (rare).
    ref_price = (
        entry.filled_avg_price or entry.limit_price or entry.stop_price
    )
    if ref_price is None or ref_price <= 0:
        return False
    # SL percent magnitude (always positive). For a long with entry
    # $11.53 and stop_loss_price $11.30 → (11.53 - 11.30) / 11.53 = 1.99%.
    # For a short, it's the symmetric rise.
    if is_long:
        sl_pct = (ref_price - entry.stop_loss_price) / ref_price * Decimal(100)
    else:
        sl_pct = (entry.stop_loss_price - ref_price) / ref_price * Decimal(100)
    if sl_pct <= 0:
        # Misconfigured (SL on the wrong side of entry). Don't fire on
        # this signal; the price comparison above already handled it.
        return False
    # Live unrealized loss in percent (negative when underwater).
    unrealized_pct = pos.unrealized_pnl / basis * Decimal(100)
    return unrealized_pct <= -sl_pct


def enforce_trader_option_sl(
    db: Session,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
) -> list[dict]:
    """Per-tick entry-point. Returns a list of triggered closes (one
    dict per closed option position) so the caller can publish events.
    Empty list = nothing fired this tick."""
    acct = db.get(BrokerAccount, broker_account_id)
    if acct is None or acct.user_id != trader_user_id:
        return []
    if acct.connection_status != "connected":
        return []

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
        positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        log.exception(
            "trader_bracket_monitor: get_positions failed for account %s",
            broker_account_id,
        )
        return []

    closures: list[dict] = []
    for pos in positions:
        if pos.instrument_type != InstrumentType.OPTION or pos.quantity == 0:
            continue
        if pos.current_price is None or pos.current_price <= 0:
            # Without a fresh mark we can't decide whether SL is
            # breached. Skip; next tick may have data.
            continue

        entry = _find_entry_with_sl(db, trader_user_id, broker_account_id, pos)
        if entry is None or entry.stop_loss_price is None:
            continue

        is_long = pos.quantity > 0
        if not _sl_breached(pos, entry, is_long):
            continue

        # In-flight guard. If a prior tick already placed a close (or
        # the original TP leg has been swapped for a market exit by
        # the user), don't fire again.
        if _has_in_flight_close(db, trader_user_id, broker_account_id, pos):
            log.debug(
                "trader_bracket_monitor: skip %s — close already in flight",
                pos.symbol,
            )
            continue

        result = _trigger_close(
            db, adapter=adapter, acct=acct,
            trader_user_id=trader_user_id, entry=entry, pos=pos,
        )
        if result is not None:
            closures.append(result)

    return closures


def _find_entry_with_sl(
    db: Session,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    pos: BrokerPosition,
) -> Order | None:
    """Most-recent FILLED entry on this exact option contract with a
    stop_loss_price set. Filters by symbol + expiry + strike + right
    so we don't accidentally pair a position with an entry for a
    different contract on the same underlying."""
    q = (
        select(Order)
        .where(
            Order.user_id == trader_user_id,
            Order.broker_account_id == broker_account_id,
            Order.instrument_type == InstrumentType.OPTION,
            Order.symbol == pos.symbol,
            Order.status == OrderStatus.FILLED,
            Order.is_closing.is_(False),
            Order.parent_order_id.is_(None),
            Order.bracket_parent_id.is_(None),
            Order.stop_loss_price.isnot(None),
        )
        .order_by(desc(Order.created_at))
        .limit(1)
    )
    if pos.option_expiry is not None:
        q = q.where(Order.option_expiry == pos.option_expiry)
    if pos.option_strike is not None:
        q = q.where(Order.option_strike == pos.option_strike)
    if pos.option_right is not None:
        q = q.where(Order.option_right == pos.option_right)
    return db.execute(q).scalar_one_or_none()


def _has_in_flight_close(
    db: Session,
    trader_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    pos: BrokerPosition,
) -> bool:
    """True if there's a still-working close (is_closing=True) for this
    exact contract. We deliberately DON'T count the TP leg itself
    here — the SL trigger is supposed to cancel + replace it. The
    filter is on rows produced by THIS monitor (bracket_leg IS NULL,
    is_closing=True). Conservative: if in doubt, skip the tick."""
    q = (
        select(Order.id)
        .where(
            Order.user_id == trader_user_id,
            Order.broker_account_id == broker_account_id,
            Order.symbol == pos.symbol,
            Order.instrument_type == InstrumentType.OPTION,
            Order.is_closing.is_(True),
            Order.bracket_leg.is_(None),
            Order.status.in_(_ALIVE_STATUSES),
        )
        .limit(1)
    )
    if pos.option_expiry is not None:
        q = q.where(Order.option_expiry == pos.option_expiry)
    if pos.option_strike is not None:
        q = q.where(Order.option_strike == pos.option_strike)
    if pos.option_right is not None:
        q = q.where(Order.option_right == pos.option_right)
    return db.execute(q).scalar_one_or_none() is not None


def _trigger_close(
    db: Session, *,
    adapter,
    acct: BrokerAccount,
    trader_user_id: uuid.UUID,
    entry: Order,
    pos: BrokerPosition,
) -> dict | None:
    """SL fired for this option position. Cancel any sibling TP leg
    that's still working, then place a SELL/BUY LIMIT close at the
    current mark (tick-rounded). Returns a summary dict, or None if
    we couldn't place the close.

    Per-step failures (TP cancel fails, close place fails) are logged
    + audited but don't crash the caller. Worst case: orphan TP leg
    that the trader can clean up manually; if both TP fills AND our
    close fires in the same window, the second is rejected for
    oversold and we audit that too — the position still gets closed.
    """
    side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    qty = abs(pos.quantity)
    limit_price = _round_close_limit(pos.current_price, side)

    # Step 1 — cancel the bracket TP leg (if it's still working).
    tp_leg = db.execute(
        select(Order).where(
            Order.bracket_parent_id == entry.id,
            Order.bracket_leg == "tp",
            Order.status.in_(_ALIVE_STATUSES),
        ).limit(1)
    ).scalar_one_or_none()
    if tp_leg is not None and tp_leg.broker_order_id:
        try:
            adapter.cancel_order(tp_leg.broker_order_id)
            tp_leg.status = OrderStatus.CANCELED
            tp_leg.closed_at = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            # Broker may have already cancelled / filled — fine, we
            # proceed with the close attempt anyway. If the TP fills
            # between this point and the broker actually receiving
            # our close, oversold rejection will surface in the audit
            # below.
            log.warning(
                "trader_bracket_monitor: cancel TP leg %s failed: %s",
                tp_leg.broker_order_id, exc,
            )

    # Step 2 — place the SL close. Persist locally first so we have an
    # id (used as client_order_id for broker-side dedup).
    local = Order(
        user_id=trader_user_id,
        broker_account_id=acct.id,
        instrument_type=InstrumentType.OPTION,
        symbol=pos.symbol,
        option_expiry=pos.option_expiry,
        option_strike=pos.option_strike,
        option_right=pos.option_right,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=limit_price,
        status=OrderStatus.PENDING,
        is_closing=True,
        fanned_out_to_subscribers=True,  # trader-only — no fanout
    )
    db.add(local)
    db.flush()

    try:
        result = adapter.place_order(
            BrokerOrderRequest(
                instrument_type=local.instrument_type,
                symbol=local.symbol,
                side=local.side,
                order_type=local.order_type,
                quantity=local.quantity,
                limit_price=local.limit_price,
                option_expiry=local.option_expiry,
                option_strike=local.option_strike,
                option_right=local.option_right,
                client_order_id=str(local.id),
                is_closing=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "trader_bracket_monitor: place SL close failed for %s on %s",
            pos.symbol, acct.id,
        )
        local.status = OrderStatus.REJECTED
        local.reject_reason = f"trader_sl_monitor_error: {exc}"[:480]
        local.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=trader_user_id,
            action="trader.bracket_sl_close_failed",
            entity_type="order", entity_id=local.id,
            metadata={
                "entry_order_id": str(entry.id),
                "symbol": pos.symbol,
                "qty": str(qty),
                "sl_price": str(entry.stop_loss_price),
                "mark": str(pos.current_price),
                "limit": str(limit_price),
                "error": str(exc)[:300],
            },
        )
        return None

    local.broker_order_id = result.broker_order_id
    local.status = result.status
    local.submitted_at = result.submitted_at
    audit.record(
        db, actor_user_id=trader_user_id,
        action="trader.bracket_sl_close_placed",
        entity_type="order", entity_id=local.id,
        metadata={
            "entry_order_id": str(entry.id),
            "symbol": pos.symbol,
            "qty": str(qty),
            "side": side.value,
            "sl_price": str(entry.stop_loss_price),
            "mark": str(pos.current_price),
            "limit": str(limit_price),
            "broker_order_id": result.broker_order_id,
        },
    )
    return {
        "symbol": pos.symbol,
        "qty": str(qty),
        "side": side.value,
        "sl_price": str(entry.stop_loss_price),
        "mark": str(pos.current_price),
        "limit": str(limit_price),
        "broker_order_id": result.broker_order_id,
        "entry_order_id": str(entry.id),
    }


__all__ = ["enforce_trader_option_sl"]
