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
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.brokers.base import BrokerOrderRequest
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services import audit
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


# US-listed-options minimum-tick rules enforced by Alpaca (and SnapTrade
# when routing to Alpaca). Sending a price off-tick gets you a 422 with
# either "limit price must be limited to 2 decimal places" or, for
# >= $3 options, an off-grid rejection. Stocks are always penny-tick.
_PENNY = Decimal("0.01")
_NICKEL = Decimal("0.05")
_OPTION_NICKEL_THRESHOLD = Decimal("3.00")


def _round_to_tick(price: Decimal, instrument_type: InstrumentType, leg: str) -> Decimal:
    """Round `price` to the smallest tick the broker will accept.

    Rounding direction is leg-aware so we never push the exit *into* a
    losing region:
      * TP (sell-limit for a long, buy-limit for a short) → round
        DOWN so we still take profit even if we shave a penny. A
        ROUND_UP here would push the limit further from the market
        and reduce fill probability.
      * SL (stop) → round UP toward the threshold side that *triggers
        earlier*, so we don't accidentally widen the stop. For longs
        the stop trips on price drop, so rounding up keeps the trigger
        at or slightly tighter than the user asked for.

    Stocks always tick at $0.01; options tick at $0.01 below $3 and
    $0.05 at $3 and above."""
    if leg == "tp":
        mode = ROUND_DOWN
    elif leg == "sl":
        mode = ROUND_UP
    else:
        mode = ROUND_HALF_UP

    if instrument_type == InstrumentType.OPTION and price >= _OPTION_NICKEL_THRESHOLD:
        tick = _NICKEL
    else:
        tick = _PENNY
    # Quantize to the tick: divide → round → multiply.
    return (price / tick).quantize(Decimal("1"), rounding=mode) * tick

# OrderStatuses we still consider "alive" — used to decide whether the
# sibling exit can be cancelled. PENDING covers the brief window between
# our DB INSERT and the broker accepting.
_ALIVE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _leg_direction(side: OrderSide, leg: str) -> Decimal:
    """+1 when a correctly-placed leg sits ABOVE entry, -1 when BELOW.
    Mirrors copy_engine._leg_direction and the frontend InlineBracketCell:
      buy+tp / sell+sl → +1 ; buy+sl / sell+tp → -1."""
    buy = side == OrderSide.BUY
    positive = (buy and leg == "tp") or (not buy and leg == "sl")
    return Decimal("1") if positive else Decimal("-1")


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

    # Copied percent bracket (subscriber with copy_trader_bracket on):
    # re-anchor the trader's percent distance onto THIS account's own
    # fill so the exit sits the same % from entry as the trader's,
    # regardless of the subscriber's fill price or multiplier. limit_price
    # first (same anchor the trader's bracket uses), else filled_avg_price.
    if entry.take_profit_pct is not None or entry.stop_loss_pct is not None:
        anchor = entry.limit_price or entry.filled_avg_price
        if anchor and anchor > 0:
            if entry.take_profit_pct is not None:
                tp_price = anchor * (1 + _leg_direction(entry.side, "tp") * entry.take_profit_pct / 100)
            if entry.stop_loss_pct is not None:
                sl_price = anchor * (1 + _leg_direction(entry.side, "sl") * entry.stop_loss_pct / 100)

    if not tp_price and not sl_price:
        return []
    if entry.broker_account_id is None:
        log.info("bracket: entry %s has no broker_account, skipping", entry.id)
        return []

    acct = db.get(BrokerAccount, entry.broker_account_id)
    if acct is None:
        return []
    # Native-bracket short-circuit applies ONLY to the trader's own entries.
    # Subscriber mirror entries (parent_order_id set) NEVER get a native
    # broker bracket — copy_engine always sends None for TP/SL on the broker
    # request — so a copied bracket on Alpaca stocks must be emulated here,
    # not skipped as "already handled natively".
    is_copied_mirror = entry.parent_order_id is not None
    if not is_copied_mirror and _uses_native_bracket(acct.broker, entry.instrument_type):
        # Alpaca's OrderClass.BRACKET already attached these legs on the
        # broker side — nothing for us to do. (Options on Alpaca are
        # NOT native — see _uses_native_bracket; those fall through to
        # the emulator path below.)
        return []

    # Idempotency: don't place the SAME leg twice. Re-deliveries of the
    # same FILLED event are common when both the listener poll and the
    # fills_sync pass land on the same transition.
    #
    # We dedup PER LEG, not as an all-or-nothing block. That serves two
    # cases at once:
    #   (a) Re-delivery — both TP and SL siblings are already alive/
    #       filled, so both are skipped and we return []. Same behaviour
    #       as the original short-circuit.
    #   (b) Bracket-modify of only one leg — the modify endpoint cancels
    #       just the changing leg, leaves the untouched one alive, and
    #       calls us. We see the surviving alive leg and DON'T re-place
    #       it, but we DO place a fresh leg on the other side using the
    #       parent's updated price.
    # Canceled / rejected children don't block anything — they're gone.
    BLOCKING = (*_ALIVE_STATUSES, OrderStatus.FILLED)
    already_rows = db.execute(
        select(Order).where(
            Order.bracket_parent_id == entry.id,
            Order.status.in_(BLOCKING),
        )
    ).scalars().all()
    already_legs = {a.bracket_leg for a in already_rows if a.bracket_leg}

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
    if tp_price and "tp" not in already_legs:
        # Round to the smallest tick the broker accepts. Without this,
        # a TP/SL computed as a percentage of entry price (e.g.
        # 2.51 × 1.02 = 2.5602) is rejected by Alpaca's options API
        # with "limit price must be limited to 2 decimal places".
        legs.append(("tp", OrderType.LIMIT, _round_to_tick(tp_price, entry.instrument_type, "tp")))
    if sl_price and "sl" not in already_legs:
        legs.append(("sl", OrderType.STOP, _round_to_tick(sl_price, entry.instrument_type, "sl")))
    if not legs:
        # Both sides are either already placed or were never requested.
        return []

    created: list[Order] = []
    for leg, otype, price in legs:
        # Skip-guard: Alpaca's options API does NOT support STOP /
        # STOP_LIMIT order types at all (only MARKET + LIMIT). Sending
        # a STOP for an option always returns code 40310000
        # ("account not eligible to trade uncovered option contracts"
        # — Alpaca's misleading framing of "this order type isn't
        # allowed on options"). Same restriction applies to SnapTrade
        # routing to Alpaca + most other SnapTrade-routed brokerages.
        #
        # Without a STOP, we can't natively place an SL leg for an
        # option at the broker. The clean fix is a price-monitor that
        # places a MARKET close when the option mark crosses sl_price
        # — left as a separate task. For now: skip placement, audit
        # the skip with a clear reason, notify the trader so they
        # know SL is unmonitored, leave the row in a CANCELED state
        # (not REJECTED — that implies the broker rejected, which
        # didn't happen because we never called the broker).
        if (
            entry.instrument_type == InstrumentType.OPTION
            and otype == OrderType.STOP
        ):
            # Alpaca options + SnapTrade-routed Alpaca options don't
            # accept STOP/STOP_LIMIT — only MARKET + LIMIT (see
            # https://alpaca.markets/docs/trading/orders/). Don't bother
            # the broker with a guaranteed rejection. The SL is still
            # enforced — `trader_bracket_monitor` runs in pnl_poller
            # and triggers a LIMIT close when the option mark crosses
            # `entry.stop_loss_price`. The audit row below is purely
            # diagnostic; no user-facing notification (the monitor
            # publishes its own when the SL actually fires).
            log.debug(
                "bracket: deferring option SL leg for entry %s to "
                "trader_bracket_monitor (broker=%s)",
                entry.id, acct.broker.value,
            )
            audit.record(
                db, actor_user_id=entry.user_id,
                action="bracket.sl_deferred_to_monitor",
                entity_type="order", entity_id=entry.id,
                metadata={
                    "entry_order_id": str(entry.id),
                    "leg": leg,
                    "sl_price": str(price),
                    "symbol": entry.symbol,
                    "broker": acct.broker.value,
                },
            )
            continue

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
            # CRITICAL: bracket exits are TRADER-ONLY. By policy
            # (see copy_engine.fanout_async) subscribers never receive
            # TP/SL — their mirrored entries are constructed with
            # take_profit_price=NULL / stop_loss_price=NULL, so when
            # the subscriber's own listener fires this emulator on
            # their mirrored entry's fill, the "if not tp_price and
            # not sl_price: return []" guard at the top short-circuits.
            # We must ALSO make sure the trader's exits themselves
            # never get broadcast — even though emulate_bracket_exits
            # doesn't call fanout, the backfill sweep in
            # trade_listener.py picks up any row matching
            # `fanned_out_to_subscribers=False AND parent_order_id IS
            # NULL AND status IN (...)`, which would otherwise match
            # these exits. Flagging them as fanned-out at creation
            # tells that sweep "fanout resolved" and keeps them off
            # the wire. Belt-and-braces: fanout_async has a
            # bracket_parent_id guard too.
            fanned_out_to_subscribers=True,
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

    Side effect: emits a trader-facing notification (persistent + SSE
    push) the FIRST time we observe a bracket leg fill for an entry —
    this is the only point in the lifecycle where we can guarantee the
    trader sees their TP/SL trigger. Done before cancelling the sibling
    so a sibling-cancel failure doesn't suppress the user-visible
    notification.
    """
    if not filled_exit.bracket_parent_id or not filled_exit.bracket_leg:
        return False
    if filled_exit.status != OrderStatus.FILLED:
        return False

    _notify_trader_of_bracket_fill(db, filled_exit)

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


def _notify_trader_of_bracket_fill(db: Session, filled_exit: Order) -> None:
    """Persist + push an in-app notification for the trader the moment
    one of their bracket legs fills.

    Idempotent: we only notify the FIRST time a leg fills for a given
    entry. The listener may re-deliver the same FILLED status across
    polls (snaptrade_listener short-circuits via ``status_changed``
    upstream, but trade_listener for Alpaca can re-call us on every
    websocket update for the same order). We check whether an audit
    row already exists for this exit before sending.

    Best-effort: any failure is swallowed and logged so the OCO cancel
    path that follows isn't blocked by a notification hiccup."""
    try:
        from app.services import notifications as notif_svc  # noqa: PLC0415

        # Dedup gate. We use a marker audit row keyed on (order id +
        # action) — present means we already notified for this fill.
        # Cheap PK-equivalent lookup.
        from app.models.audit_log import AuditLog  # noqa: PLC0415
        already = db.execute(
            select(AuditLog.id).where(
                AuditLog.action == "bracket.trader_notified",
                AuditLog.entity_id == str(filled_exit.id),
            ).limit(1)
        ).scalar_one_or_none()
        if already is not None:
            return

        leg_label = "take-profit" if filled_exit.bracket_leg == "tp" else "stop-loss"
        # Price the leg was set at. Fall back to the actual fill price
        # if the trigger price wasn't recorded on the row for some reason.
        trigger_price = (
            filled_exit.limit_price
            if filled_exit.bracket_leg == "tp"
            else filled_exit.stop_price
        )
        fill_price = filled_exit.filled_avg_price
        qty = filled_exit.filled_quantity or filled_exit.quantity
        symbol = filled_exit.symbol or "position"

        # Realised-P&L %, anchored on the entry's limit_price (same
        # anchor the trader saw when they set the bracket "5% / 10%"),
        # falling back to filled_avg_price for market entries. Signed:
        # positive on TP fills, negative on SL fills, regardless of
        # whether the entry was long or short.
        pnl_pct_str: str | None = None
        parent: "Order | None" = None
        if filled_exit.bracket_parent_id is not None:
            parent = db.get(Order, filled_exit.bracket_parent_id)
        if parent is not None and fill_price is not None:
            entry_price = parent.limit_price or parent.filled_avg_price
            if entry_price and entry_price > 0:
                was_long = parent.side == OrderSide.BUY
                direction = Decimal("1") if was_long else Decimal("-1")
                pct = ((fill_price - entry_price) / entry_price) * Decimal("100") * direction
                pnl_pct_str = f"{pct.quantize(Decimal('0.01'))}"

        # Trader-readable message. Includes BOTH the trigger and the
        # actual fill so they can spot slippage at a glance. The
        # realised P&L tagline anchors the "what does this mean for
        # me" answer right in the notification.
        msg_parts = [f"{symbol} {leg_label} hit"]
        if trigger_price is not None:
            msg_parts.append(f"at ${trigger_price}")
        if fill_price is not None and fill_price != trigger_price:
            msg_parts.append(f"(filled ${fill_price})")
        msg_parts.append(f"— {qty} closed.")
        if pnl_pct_str is not None:
            msg_parts.append(f"Realised P&L: {pnl_pct_str}%.")
        message = " ".join(msg_parts)

        notif_svc.create_notification(
            db,
            user_id=filled_exit.user_id,
            type=f"bracket.{filled_exit.bracket_leg}_filled",
            message=message,
            metadata={
                "entry_order_id": str(filled_exit.bracket_parent_id),
                "exit_order_id": str(filled_exit.id),
                "leg": filled_exit.bracket_leg,
                "symbol": symbol,
                "qty": str(qty) if qty is not None else None,
                "trigger_price": str(trigger_price) if trigger_price is not None else None,
                "fill_price": str(fill_price) if fill_price is not None else None,
                "pnl_pct": pnl_pct_str,
                "broker_order_id": filled_exit.broker_order_id,
            },
        )

        # Stamp the dedup marker so re-deliveries don't double-notify.
        audit.record(
            db, actor_user_id=filled_exit.user_id,
            action="bracket.trader_notified",
            entity_type="order", entity_id=filled_exit.id,
            metadata={
                "leg": filled_exit.bracket_leg,
                "symbol": symbol,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "bracket: trader notification failed for exit %s — "
            "OCO cancel will still run",
            filled_exit.id,
        )


# Re-export the public surface so listeners can `from app.services.bracket_emulator
# import emulate_bracket_exits, cancel_sibling_on_fill` without touching the
# private helpers above.
__all__ = ["emulate_bracket_exits", "cancel_sibling_on_fill"]
