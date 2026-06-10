"""Bracket-order emulation for brokers without native OCO support.

Why this exists
---------------
Alpaca's order API natively supports ``OrderClass.BRACKET`` — pass a parent
entry plus attached TP (LimitOrderRequest) and SL (StopLossRequest) legs
and Alpaca handles "place exits when parent fills, OCO when one fires."

SnapTrade (and our IBKR / Webull integrations) don't expose that. To give
traders the same UX across all brokers, we emulate brackets ourselves:

  1. The trade endpoint stores ``take_profit_price`` and ``stop_loss_price``
     on the entry Order row but DOES NOT forward them to the adapter for
     non-Alpaca brokers — the entry goes through as a plain order.
  2. The per-broker listener watches for the entry's status to transition
     to FILLED, then calls :func:`emulate_bracket_exits` to place the TP
     (LIMIT) + SL (STOP) exits on the opposite side, sized to the entry's
     actual ``filled_quantity``.
  3. The same listener calls :func:`cancel_sibling_on_fill` when any
     bracket-leg exit fills, so the surviving leg gets cancelled — that's
     our OCO emulation.

Both entry-points are idempotent — calling them twice with the same input
does nothing the second time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.brokers.base import BrokerOrderRequest
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.services import audit
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# OrderStatuses we still consider "alive" — used to decide whether the
# sibling exit can be cancelled. PENDING covers the brief window between
# our DB INSERT and the broker accepting.
_ALIVE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _uses_native_bracket(broker: BrokerName, instrument_type: "InstrumentType | None" = None) -> bool:
    """True only when the (broker, instrument) pair supports server-side
    bracket / OCO orders. Currently that's Alpaca **stocks** only:
    Alpaca's options API explicitly rejects complex orders (error
    42210000 — "complex orders not supported for options trading"), so
    options on Alpaca must use the emulator like SnapTrade does."""
    from app.models.order import InstrumentType  # noqa: PLC0415
    if broker != BrokerName.ALPACA:
        return False
    if instrument_type == InstrumentType.OPTION:
        return False
    return True


def emulate_bracket_exits(db: Session, entry: Order) -> list[Order]:
    """Place TP / SL exit orders for an entry that just filled.

    Returns the list of exit Orders that were created (possibly empty).
    No-ops in these cases:
      * Entry hasn't actually filled yet.
      * Entry has no bracket prices set.
      * Entry's broker handles bracket natively (Alpaca).
      * Exits already exist for this entry (idempotency guard).
      * Entry is itself a bracket-exit leg (don't bracket the bracket).

    The caller is responsible for ``db.commit()`` — we only ``flush`` so
    the new Order ids are available for the audit payload.
    """
    if entry.bracket_leg is not None:
        # This is itself an exit leg — never bracket it.
        return []
    if entry.status != OrderStatus.FILLED:
        return []
    tp_price = entry.take_profit_price
    sl_price = entry.stop_loss_price
    if not tp_price and not sl_price:
        return []
    if entry.broker_account_id is None:
        log.info("bracket: entry %s has no broker_account, skipping", entry.id)
        return []

    acct = db.get(BrokerAccount, entry.broker_account_id)
    if acct is None:
        return []
    if _uses_native_bracket(acct.broker, entry.instrument_type):
        # Alpaca's OrderClass.BRACKET already attached these legs on the
        # broker side — nothing for us to do. (Options on Alpaca are
        # NOT native — see _uses_native_bracket; those fall through to
        # the emulator path below.)
        return []

    # Idempotency: don't place exits twice. Re-deliveries of the same
    # FILLED event are common when both the listener poll and the
    # fills_sync pass land on the same transition.
    already = db.execute(
        select(Order).where(Order.bracket_parent_id == entry.id)
    ).scalars().all()
    if already:
        return []

    qty = entry.filled_quantity or entry.quantity
    if not qty or qty <= 0:
        log.warning("bracket: entry %s has no usable quantity, skipping", entry.id)
        return []

    exit_side = OrderSide.SELL if entry.side == OrderSide.BUY else OrderSide.BUY

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
    except Exception as exc:  # noqa: BLE001
        log.exception("bracket: creds/adapter for entry %s failed: %s", entry.id, exc)
        return []

    legs: list[tuple[str, OrderType, Decimal]] = []
    if tp_price:
        legs.append(("tp", OrderType.LIMIT, tp_price))
    if sl_price:
        legs.append(("sl", OrderType.STOP, sl_price))

    created: list[Order] = []
    for leg, otype, price in legs:
        exit_order = Order(
            user_id=entry.user_id,
            broker_account_id=acct.id,
            bracket_parent_id=entry.id,
            bracket_leg=leg,
            instrument_type=entry.instrument_type,
            symbol=entry.symbol,
            option_expiry=entry.option_expiry,
            option_strike=entry.option_strike,
            option_right=entry.option_right,
            side=exit_side,
            order_type=otype,
            quantity=qty,
            limit_price=price if otype == OrderType.LIMIT else None,
            stop_price=price if otype == OrderType.STOP else None,
            status=OrderStatus.PENDING,
            is_closing=True,
            # Mirror the entry's fanout flag — if subscribers mirrored
            # the entry they'll also mirror the exits (when their own
            # listener fires the emulator for their copy of the entry).
            fanned_out_to_subscribers=False,
        )
        db.add(exit_order)
        db.flush()

        try:
            result = adapter.place_order(
                BrokerOrderRequest(
                    instrument_type=exit_order.instrument_type,
                    symbol=exit_order.symbol,
                    side=exit_order.side,
                    order_type=exit_order.order_type,
                    quantity=exit_order.quantity,
                    limit_price=exit_order.limit_price,
                    stop_price=exit_order.stop_price,
                    option_expiry=exit_order.option_expiry,
                    option_strike=exit_order.option_strike,
                    option_right=exit_order.option_right,
                    client_order_id=str(exit_order.id),
                    is_closing=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "bracket: place %s exit for entry %s failed: %s", leg, entry.id, exc
            )
            exit_order.status = OrderStatus.REJECTED
            exit_order.reject_reason = f"bracket_exit_error: {exc}"[:480]
            exit_order.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=entry.user_id,
                action="bracket.exit_failed",
                entity_type="order", entity_id=exit_order.id,
                metadata={
                    "entry_order_id": str(entry.id),
                    "leg": leg,
                    "price": str(price),
                    "error": str(exc)[:300],
                },
            )
            created.append(exit_order)
            continue

        exit_order.broker_order_id = result.broker_order_id
        exit_order.status = result.status
        exit_order.submitted_at = result.submitted_at
        audit.record(
            db, actor_user_id=entry.user_id,
            action="bracket.exit_placed",
            entity_type="order", entity_id=exit_order.id,
            metadata={
                "entry_order_id": str(entry.id),
                "leg": leg,
                "price": str(price),
                "broker_order_id": result.broker_order_id,
            },
        )
        created.append(exit_order)

    return created


def cancel_sibling_on_fill(db: Session, filled_exit: Order) -> bool:
    """OCO emulation — when one bracket leg fills, cancel the other.

    Returns True if a sibling was cancelled, False otherwise (no sibling,
    sibling already terminalized, or cancel call failed).

    Safe to call on any order; it short-circuits if the order isn't a
    bracket-exit leg or hasn't actually filled.
    """
    if not filled_exit.bracket_parent_id or not filled_exit.bracket_leg:
        return False
    if filled_exit.status != OrderStatus.FILLED:
        return False

    sibling = db.execute(
        select(Order).where(
            Order.bracket_parent_id == filled_exit.bracket_parent_id,
            Order.bracket_leg != filled_exit.bracket_leg,
            Order.status.in_(_ALIVE_STATUSES),
        )
    ).scalar_one_or_none()
    if sibling is None:
        return False

    # If the sibling never made it past PENDING we don't have a broker id
    # to cancel — just mark it locally.
    if not sibling.broker_order_id:
        sibling.status = OrderStatus.CANCELED
        sibling.closed_at = datetime.now(timezone.utc)
        return True

    acct = db.get(BrokerAccount, sibling.broker_account_id)
    if acct is None:
        sibling.status = OrderStatus.CANCELED
        sibling.closed_at = datetime.now(timezone.utc)
        return True

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
        adapter.cancel_order(sibling.broker_order_id)
    except Exception as exc:  # noqa: BLE001
        # Broker may have already cancelled / filled it between our
        # listener's two polls — that's fine, fills_sync will reconcile.
        log.warning(
            "bracket: cancel sibling %s (broker_id=%s) failed: %s",
            sibling.id, sibling.broker_order_id, exc,
        )
        audit.record(
            db, actor_user_id=filled_exit.user_id,
            action="bracket.sibling_cancel_failed",
            entity_type="order", entity_id=sibling.id,
            metadata={
                "entry_order_id": str(filled_exit.bracket_parent_id),
                "filled_leg": filled_exit.bracket_leg,
                "cancelled_leg": sibling.bracket_leg,
                "error": str(exc)[:300],
            },
        )
        return False

    sibling.status = OrderStatus.CANCELED
    sibling.closed_at = datetime.now(timezone.utc)
    audit.record(
        db, actor_user_id=filled_exit.user_id,
        action="bracket.sibling_cancelled",
        entity_type="order", entity_id=sibling.id,
        metadata={
            "entry_order_id": str(filled_exit.bracket_parent_id),
            "filled_leg": filled_exit.bracket_leg,
            "cancelled_leg": sibling.bracket_leg,
        },
    )
    return True


# Re-export the public surface so listeners can `from app.services.bracket_emulator
# import emulate_bracket_exits, cancel_sibling_on_fill` without touching the
# private helpers above.
__all__ = ["emulate_bracket_exits", "cancel_sibling_on_fill"]
