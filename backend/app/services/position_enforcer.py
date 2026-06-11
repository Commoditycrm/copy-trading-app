"""Per-position TP/SL enforcement.

Triggered by ``pnl_poller`` for any subscriber broker account whose
owner has ``SubscriberSettings.position_tp_pct`` or ``position_sl_pct``
set. For every open position on the account, computes

    pct = unrealized_pnl / abs(cost_basis) * 100

and closes the position at market when the percentage breaches the
configured TP (>= +tp_pct) or SL (<= -sl_pct).

Per-position only — a triggered close does NOT pause copy or affect
other positions. Distinct from:
  * daily_loss_limit_pct / daily_profit_limit_pct — pause copy when
    REALIZED daily P&L hits a % of the day-start balance.
  * auto_liquidation_limit — flatten the WHOLE account when UNREALIZED
    daily profit hits a USD ceiling AND disable copy.

Best-effort + per-position-isolated: a failure on one symbol doesn't
abort the rest. The poller catches any exception we raise so a bad
enforcement never corrupts the rest of the tick.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy import select
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
from app.models.settings import SubscriberSettings
from app.services import audit
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


# Working orders we cancel for the symbol before closing — leftover
# bracket exits or limits would otherwise reject the close as oversold
# or both fill (double the close).
_CANCELLABLE = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)

# US-listed options tick rules — same as bracket_emulator's _round_to_tick.
# Stocks always penny; options $0.01 under $3, $0.05 at/above $3.
_PENNY = Decimal("0.01")
_NICKEL = Decimal("0.05")
_OPTION_NICKEL_THRESHOLD = Decimal("3.00")


def _round_limit_for_close(
    price: Decimal, instrument_type: InstrumentType, side: OrderSide,
) -> Decimal:
    """Round ``price`` to the broker's tick for use as a close limit.

    Rounding direction is fill-friendly: when SELLING (closing a long)
    we round DOWN so the limit sits slightly below current and is
    likely to fill; when BUYING (closing a short) we round UP so the
    limit sits slightly above. A "perfectly aligned" current_price
    isn't enough — sub-penny option marks always need quantization
    before the broker will accept them."""
    if side == OrderSide.SELL:
        mode = ROUND_DOWN
    elif side == OrderSide.BUY:
        mode = ROUND_UP
    else:
        mode = ROUND_HALF_UP

    if instrument_type == InstrumentType.OPTION and price >= _OPTION_NICKEL_THRESHOLD:
        tick = _NICKEL
    else:
        tick = _PENNY
    return (price / tick).quantize(Decimal("1"), rounding=mode) * tick


def _position_pct(pos: BrokerPosition) -> Decimal | None:
    """Return position unrealized P&L as a percent of |cost_basis|.

    None when the inputs aren't computable (broker didn't report
    cost_basis, or it's zero — happens briefly between order fill and
    position-snapshot refresh, and for fully-closed positions sitting at
    qty=0 in some brokers' responses). Caller skips in that case."""
    if pos.unrealized_pnl is None or pos.cost_basis is None:
        return None
    basis = abs(pos.cost_basis)
    if basis == 0:
        return None
    # Decimal division produces many trailing digits (e.g.
    # -13.54838709677419354838...%) which then leaks into the SSE
    # event, the toast, the in-app notification, and the audit row.
    # Round to 2 decimal places — same precision the user enters
    # their TP/SL at, and what they'd expect to see in a report.
    raw = (pos.unrealized_pnl / basis) * Decimal(100)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def enforce_position_tp_sl(
    db: Session,
    subscriber_user_id: uuid.UUID,
    broker_account_id: uuid.UUID,
    *,
    positions: list[BrokerPosition] | None = None,
) -> list[dict]:
    """Close every open position on `broker_account_id` whose unrealized
    P&L percent breached the subscriber's configured TP or SL.

    ``positions`` may be passed in by the caller (pnl_poller already
    fetches them for unrelated checks); if omitted we fetch ourselves.

    Returns a list of close summaries, one per closed position:
        [{"symbol", "leg" ("tp"|"sl"), "pct", "qty", "broker_order_id"}, ...]
    Empty list when nothing was closed.
    """
    s = db.get(SubscriberSettings, subscriber_user_id)
    if s is None:
        return []
    tp = s.position_tp_pct
    sl = s.position_sl_pct
    if tp is None and sl is None:
        return []

    acct = db.get(BrokerAccount, broker_account_id)
    if acct is None or acct.user_id != subscriber_user_id:
        return []
    if acct.connection_status != "connected":
        return []

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
    except Exception:  # noqa: BLE001
        log.exception(
            "position_enforcer: adapter init failed for account %s", broker_account_id
        )
        return []

    if positions is None:
        try:
            positions = adapter.get_positions()
        except Exception:  # noqa: BLE001
            log.exception(
                "position_enforcer: get_positions failed for account %s",
                broker_account_id,
            )
            return []

    closures: list[dict] = []
    for pos in positions:
        if pos.quantity == 0:
            continue
        pct = _position_pct(pos)
        if pct is None:
            continue
        # Direction:
        #   pct >= +tp  → take profit (winner)
        #   pct <= -sl  → stop loss (loser)
        # Symmetric for longs and shorts because unrealized_pnl is the
        # P&L in dollars and cost_basis is the |entry$|, regardless of
        # direction. A short whose unrealized_pnl is positive still
        # registers a positive pct here, so a "winning" short trips the
        # TP rule just like a winning long.
        leg: str | None = None
        if tp is not None and pct >= tp:
            leg = "tp"
        elif sl is not None and pct <= -sl:
            leg = "sl"
        if leg is None:
            continue

        # In-flight guard. The poller ticks every 10s; a market close
        # we place THIS tick may not be filled by the broker before
        # NEXT tick. If we re-entered _close_one for the same position
        # we'd (a) cancel our own previous close in the cleanup step
        # and (b) place a duplicate close. Skip the path entirely when
        # there's already a working close (is_closing=True, status in
        # _CANCELLABLE) for this symbol on this account — the next
        # tick will either see the position gone (close filled → done)
        # or see it again (close cancelled/rejected → re-trigger).
        in_flight_close = db.execute(
            select(Order.id).where(
                Order.user_id == subscriber_user_id,
                Order.broker_account_id == acct.id,
                Order.symbol == pos.symbol,
                Order.instrument_type == pos.instrument_type,
                Order.is_closing.is_(True),
                Order.status.in_(_CANCELLABLE),
            ).limit(1)
        ).scalar_one_or_none()
        if in_flight_close is not None:
            log.debug(
                "position_enforcer: skipping %s — close already in flight (order %s)",
                pos.symbol, in_flight_close,
            )
            continue

        result = _close_one(
            db,
            adapter=adapter,
            acct=acct,
            subscriber_user_id=subscriber_user_id,
            pos=pos,
            leg=leg,
            pct=pct,
            tp=tp,
            sl=sl,
        )
        if result is not None:
            closures.append(result)

    return closures


def _close_one(
    db: Session,
    *,
    adapter,
    acct: BrokerAccount,
    subscriber_user_id: uuid.UUID,
    pos: BrokerPosition,
    leg: str,
    pct: Decimal,
    tp: Decimal | None,
    sl: Decimal | None,
) -> dict | None:
    """Cancel any working orders for this symbol on the account, then
    place a market close. Returns a summary dict, or None if we
    couldn't place the close (so the caller skips emitting an event)."""

    # 1. Cancel working orders for this symbol. Otherwise the close can
    #    race a leftover bracket exit / limit and end up double-closing
    #    or being rejected for oversold.
    open_orders = db.execute(
        select(Order).where(
            Order.user_id == subscriber_user_id,
            Order.broker_account_id == acct.id,
            Order.status.in_(_CANCELLABLE),
            Order.broker_order_id.isnot(None),
            Order.symbol == pos.symbol,
            Order.instrument_type == pos.instrument_type,
        )
    ).scalars().all()
    for o in open_orders:
        try:
            adapter.cancel_order(o.broker_order_id)
            o.status = OrderStatus.CANCELED
            o.closed_at = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "position_enforcer: cancel %s for %s failed: %s",
                o.broker_order_id, pos.symbol, exc,
            )

    # 2. Place the close. Side is opposite of position direction.
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    qty = abs(pos.quantity)

    # Order type and limit price are instrument-aware:
    #   * STOCKS: MARKET — Alpaca accepts these any time, executes at
    #     the inside; this is the simplest, most reliable close.
    #   * OPTIONS: LIMIT priced at pos.current_price (tick-rounded).
    #     Alpaca's options API rejects market orders when there's no
    #     live quote (code 40310000, "please reenter with a limit") —
    #     after-hours, low-liquidity strikes, every illiquid contract.
    #     A limit at the current mark is the broker-recommended fix
    #     and works in every market state.
    if pos.instrument_type == InstrumentType.OPTION:
        if pos.current_price is None or pos.current_price <= 0:
            # Without a valid mark we have no defensible limit price.
            # Don't fire a blind close — log it, audit it, retry next
            # tick (where we may have a fresh price). The in-flight
            # guard upstream means we won't keep spamming the broker
            # since we never placed an order this round.
            log.info(
                "position_enforcer: skipping %s close — no current_price "
                "(broker may be after-hours or SnapTrade-cached at $0); "
                "will retry next tick",
                pos.symbol,
            )
            audit.record(
                db, actor_user_id=subscriber_user_id,
                action=f"subscriber.position_{leg}_close_deferred",
                entity_type="broker_account", entity_id=acct.id,
                metadata={
                    "symbol": pos.symbol,
                    "reason": "no_current_price",
                    "current_price": str(pos.current_price)
                        if pos.current_price is not None else None,
                    "leg": leg,
                    "pct": str(pct),
                },
            )
            return None
        limit_price: Decimal | None = _round_limit_for_close(
            pos.current_price, pos.instrument_type, reverse_side,
        )
        order_type = OrderType.LIMIT
    else:
        limit_price = None
        order_type = OrderType.MARKET

    local = Order(
        user_id=subscriber_user_id,
        broker_account_id=acct.id,
        instrument_type=pos.instrument_type,
        symbol=pos.symbol,
        option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
        option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
        option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
        side=reverse_side,
        order_type=order_type,
        quantity=qty,
        limit_price=limit_price,
        status=OrderStatus.PENDING,
        is_closing=True,
        # This is a subscriber-initiated close (auto-triggered, but
        # still on the subscriber's account). It must NOT be fanned out
        # — that would push close orders to other subscribers who don't
        # share this position. Mark resolved so the backfill sweep
        # skips it.
        fanned_out_to_subscribers=True,
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
            "position_enforcer: close %s (%s) on account %s failed",
            pos.symbol, leg, acct.id,
        )
        local.status = OrderStatus.REJECTED
        local.reject_reason = f"position_{leg}_error: {exc}"[:480]
        local.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=subscriber_user_id,
            action=f"subscriber.position_{leg}_close_failed",
            entity_type="order", entity_id=local.id,
            metadata={
                "symbol": pos.symbol,
                "qty": str(qty),
                "pct": str(pct),
                "tp": str(tp) if tp is not None else None,
                "sl": str(sl) if sl is not None else None,
                "error": str(exc)[:300],
            },
        )
        return None

    local.broker_order_id = result.broker_order_id
    local.status = result.status
    local.submitted_at = result.submitted_at
    audit.record(
        db, actor_user_id=subscriber_user_id,
        action=f"subscriber.position_{leg}_closed",
        entity_type="order", entity_id=local.id,
        metadata={
            "symbol": pos.symbol,
            "side": reverse_side.value,
            "qty": str(qty),
            "pct": str(pct),
            "tp": str(tp) if tp is not None else None,
            "sl": str(sl) if sl is not None else None,
            "broker_order_id": result.broker_order_id,
        },
    )
    return {
        "symbol": pos.symbol,
        "leg": leg,
        "pct": str(pct),
        "qty": str(qty),
        "broker_order_id": result.broker_order_id,
    }


__all__ = ["enforce_position_tp_sl"]
