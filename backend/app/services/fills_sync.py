"""Sync filled trades from Alpaca into our local fills + orders tables.

For each FILL activity Alpaca reports, we:
  1. Dedup by activity.id (mapped to Fill.broker_fill_id).
  2. Create a synthetic Order row (status=FILLED) — activities don't always
     carry a usable order_id linkage, so we treat each activity as a
     self-contained order. This means fills from external trades (placed
     in Alpaca's UI directly) also surface in our Trades / Calendar.
  3. Insert a Fill row attached to that Order.

Caller commits the session.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.models.broker_account import BrokerAccount
from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# Grace window before fills_sync will SYNTHESIZE an order for a fill it can't
# match to an existing row. A just-happened fill with no matching order is
# almost always an order placed through our app whose row hasn't committed its
# broker_order_id yet (the place endpoint commits only after the broker
# round-trip). Synthesizing here would duplicate that order — and then fan the
# duplicate out to subscribers. So we skip unmatched fills younger than this;
# the next sync either matches by broker_order_id (app row now committed) or, if
# the fill is genuinely external (placed in the broker's own UI), synthesizes it
# once it ages past the window. The only cost is a short delay before truly
# external fills appear.
_SYNTH_ORDER_GRACE = timedelta(seconds=90)


@dataclass
class SyncResult:
    fills_added: int
    orders_added: int
    activities_seen: int
    skipped: int


def _dec(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """SDK responses are pydantic-ish objects but sometimes dicts; tolerant lookup."""
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


_NON_TERMINAL_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _refresh_open_orders(db: Session, acct: BrokerAccount, adapter: Any) -> int:
    """Poll the broker's order endpoint for every non-terminal order on this
    account. Alpaca's order resource updates instantly on fill, while the
    activities feed can lag minutes. This keeps Order History fresh without
    waiting for activities."""
    open_orders = list(db.execute(
        select(Order).where(
            Order.broker_account_id == acct.id,
            Order.status.in_(_NON_TERMINAL_STATUSES),
            Order.broker_order_id.isnot(None),
        )
    ).scalars())

    refreshed = 0
    # Bracket exit legs that flip to FILLED here closed a position. Subscribers
    # have no real-time fill listener, so this poll-driven sync is the only
    # place we learn a copied TP/SL hit — surface it as an SSE so the positions
    # table can refresh live instead of only on manual reload.
    newly_closed: list[Order] = []
    # Mirror ENTRIES that just transitioned — used to fire/cancel any close we
    # deferred behind them (a close that arrived before its entry filled).
    filled_entries: list[Order] = []
    dead_entries: list[Order] = []
    _TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)
    for order in open_orders:
        try:
            res = adapter.get_order(order.broker_order_id)
        except Exception:  # noqa: BLE001
            # Order may have been cancelled at the broker side or the id is
            # stale — don't fail the whole sync over one bad lookup.
            continue
        prev_status = order.status
        changed = False
        if res.status != order.status:
            order.status = res.status
            changed = True
        if res.filled_quantity is not None and _dec(res.filled_quantity) != _dec(order.filled_quantity):
            order.filled_quantity = res.filled_quantity
            changed = True
        if res.filled_avg_price is not None and _dec(res.filled_avg_price) != _dec(order.filled_avg_price):
            order.filled_avg_price = res.filled_avg_price
            changed = True
        if res.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED) and order.closed_at is None:
            order.closed_at = datetime.now(timezone.utc)
            changed = True
        if (
            res.status == OrderStatus.FILLED
            and prev_status != OrderStatus.FILLED
            and order.bracket_leg is not None
        ):
            newly_closed.append(order)
        # Track a mirror ENTRY transition for the deferred-close hook below.
        if changed and order.parent_order_id is not None and not order.is_closing:
            if res.status == OrderStatus.FILLED and prev_status != OrderStatus.FILLED:
                filled_entries.append(order)
            elif res.status in _TERMINAL and prev_status not in _TERMINAL:
                dead_entries.append(order)
        if changed:
            refreshed += 1

    for order in newly_closed:
        try:
            from app.services import events as _events  # noqa: PLC0415
            _events.publish(order.user_id, {
                "type": "position.auto_closed",
                "leg": order.bracket_leg,
                "symbol": order.symbol,
                "qty": str(order.filled_quantity) if order.filled_quantity is not None else None,
                "broker": acct.broker.value,
            })
        except Exception:  # noqa: BLE001
            log.exception(
                "fills_sync: position.auto_closed publish failed for order=%s", order.id
            )

    # Deferred-close hook (own sessions inside): a subscriber entry that just
    # FILLED fires any close parked behind it; one that DIED cancels it.
    if filled_entries or dead_entries:
        from app.services import copy_engine  # noqa: PLC0415
        for e in filled_entries:
            try:
                copy_engine.fire_deferred_closes_for_entry(e)
            except Exception:  # noqa: BLE001
                log.exception("fills_sync: fire deferred closes failed for entry=%s", e.id)
        for e in dead_entries:
            try:
                copy_engine.cancel_deferred_closes_for_entry(e)
            except Exception:  # noqa: BLE001
                log.exception("fills_sync: cancel deferred closes failed for entry=%s", e.id)
    return refreshed


def sync_account_fills(db: Session, acct: BrokerAccount) -> SyncResult:
    """Pull activities for `acct` from its broker and upsert fills locally.
    Idempotent — re-runs are safe."""
    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    if not isinstance(adapter, AlpacaAdapter):
        return SyncResult(fills_added=0, orders_added=0, activities_seen=0, skipped=0)

    # Fast path: refresh status/qty/price for orders we placed that haven't
    # terminalized yet. Catches fills the moment they happen, instead of
    # waiting for the activities feed.
    _refresh_open_orders(db, acct, adapter)

    activities = adapter.list_recent_activities()

    fills_added = 0
    orders_added = 0
    skipped = 0

    # Pre-fetch existing fill ids for this account so we don't round-trip
    # the DB once per activity.
    existing: set[str] = set(
        r[0] for r in db.execute(
            select(Fill.broker_fill_id)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.broker_account_id == acct.id, Fill.broker_fill_id.isnot(None))
        ).all()
    )

    for entry in activities:
        # Alpaca's account-activities endpoint returns mixed types.
        # We only care about FILL events. activity_type is e.g. "FILL", "DIV", "FEE".
        atype = str(_attr(entry, "activity_type")).upper()
        if atype != "FILL":
            skipped += 1
            continue

        # FILL fields: id, transaction_time, type (fill/partial_fill), price,
        # qty, side (buy/sell), symbol, order_id, cum_qty, leaves_qty, ...
        activity_id = str(_attr(entry, "id") or "")
        if not activity_id or activity_id in existing:
            skipped += 1
            continue

        side_raw = str(_attr(entry, "side") or "").lower()
        if side_raw not in ("buy", "sell"):
            skipped += 1
            continue
        side = OrderSide.BUY if side_raw == "buy" else OrderSide.SELL

        symbol_full = str(_attr(entry, "symbol") or "")
        if not symbol_full:
            skipped += 1
            continue

        # OCC option symbols are 21 chars (root padded to 6 + 6 date + 1 cp + 8 strike).
        # Heuristic: anything 18+ chars with C/P at position -9 is an option.
        is_option = len(symbol_full) >= 18 and symbol_full[-9] in ("C", "P")
        if is_option:
            # Parse OCC: ROOT(6) + YYMMDD(6) + CP(1) + STRIKE*1000(8)
            # The root might be padded with trailing chars; just split by position.
            ticker_root = symbol_full[:-15].strip()
            yymmdd = symbol_full[-15:-9]
            cp = symbol_full[-9]
            strike_str = symbol_full[-8:]
            from datetime import date as _date
            try:
                expiry = _date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
                strike = Decimal(int(strike_str)) / Decimal(1000)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            from app.models.order import OptionRight
            instrument = InstrumentType.OPTION
            display_symbol = ticker_root
            option_expiry = expiry
            option_strike = strike
            option_right = OptionRight.CALL if cp == "C" else OptionRight.PUT
        else:
            instrument = InstrumentType.STOCK
            display_symbol = symbol_full.upper()
            option_expiry = None
            option_strike = None
            option_right = None

        units = _dec(_attr(entry, "qty"))
        price = _dec(_attr(entry, "price"))
        if units <= 0 or price <= 0:
            skipped += 1
            continue

        trade_at = _as_dt(_attr(entry, "transaction_time")) or datetime.now(timezone.utc)

        # If the activity references a broker order_id that matches an Order
        # we already have, update THAT order in place. Otherwise (external
        # trades placed in Alpaca's UI directly, or pre-existing fills) create
        # a synthetic order so the activity still surfaces in the UI.
        #
        # Match by (user_id, broker_order_id) — NOT broker_account_id. When a
        # broker is deleted and reconnected, the old rows survive with
        # broker_account_id=NULL (ON DELETE SET NULL) under a NEW account id.
        # Scoping the match to the current account would miss them and
        # re-import the whole history as duplicates on every reconnect.
        # Matching by broker_order_id (globally unique per broker) finds the
        # existing row and we re-adopt it onto the reconnected account.
        broker_oid = str(_attr(entry, "order_id") or "") or None
        order: Order | None = None
        if broker_oid:
            order = db.execute(
                select(Order).where(
                    Order.user_id == acct.user_id,
                    Order.broker_order_id == broker_oid,
                ).order_by(Order.created_at.asc()).limit(1)
            ).scalar_one_or_none()
            # Re-adopt an orphaned (or differently-accounted) match onto the
            # account we're syncing now, so the row reconnects cleanly.
            if order is not None and order.broker_account_id != acct.id:
                order.broker_account_id = acct.id

        if order is None:
            # Race guard: don't synthesize a duplicate for an in-flight
            # app-placed order whose row hasn't committed yet. Skip this fill
            # for now; a later sync matches it by broker_order_id (or
            # synthesizes it once it's clearly external — older than the grace
            # window). We deliberately DON'T add activity_id to `existing`, so
            # the next sync re-processes it.
            if (datetime.now(timezone.utc) - trade_at) < _SYNTH_ORDER_GRACE:
                skipped += 1
                continue
            order = Order(
                user_id=acct.user_id,
                broker_account_id=acct.id,
                instrument_type=instrument,
                symbol=display_symbol,
                option_expiry=option_expiry,
                option_strike=option_strike,
                option_right=option_right,
                side=side,
                order_type=OrderType.MARKET,
                quantity=units,
                status=OrderStatus.FILLED,
                broker_order_id=broker_oid or activity_id,
                filled_quantity=units,
                filled_avg_price=price,
                submitted_at=trade_at,
                closed_at=trade_at,
            )
            db.add(order)
            db.flush()
            orders_added += 1
        else:
            # Recompute filled qty + volume-weighted avg from the AUTHORITATIVE
            # Fill rows (deduped by broker_fill_id), rather than ACCUMULATING
            # onto order.filled_quantity. Accumulating double-counted any order
            # whose place response / listener had already set filled_quantity:
            # e.g. an Alpaca market order that fills instantly is created with
            # filled_quantity=1, then this fill activity added another 1 → 2.
            # Summing the existing Fill rows (which exclude the one we're about
            # to add) plus `units` gives the true cumulative fill.
            prior_qty, prior_notional = db.execute(
                select(
                    func.coalesce(func.sum(Fill.quantity), 0),
                    func.coalesce(func.sum(Fill.quantity * Fill.price), 0),
                ).where(Fill.order_id == order.id)
            ).one()
            prior_qty = _dec(prior_qty)
            prior_notional = _dec(prior_notional)
            new_qty = prior_qty + units
            new_notional = prior_notional + units * price
            # Clamp to the order quantity — you can never fill MORE than you
            # ordered. Guards against any residual duplicate fill activities.
            order_qty = _dec(order.quantity)
            order.filled_quantity = min(new_qty, order_qty) if order_qty > 0 else new_qty
            order.filled_avg_price = (new_notional / new_qty) if new_qty > 0 else price
            # Mark filled if this completes the order quantity; otherwise
            # partial-fill. Float-tolerant compare (Decimal).
            if new_qty >= _dec(order.quantity):
                order.status = OrderStatus.FILLED
                order.closed_at = trade_at
            else:
                order.status = OrderStatus.PARTIALLY_FILLED

        fill = Fill(
            order_id=order.id,
            quantity=units,
            price=price,
            fee=Decimal(0),
            filled_at=trade_at,
            broker_fill_id=activity_id,
        )
        db.add(fill)
        fills_added += 1
        existing.add(activity_id)

    acct.last_activity_sync_at = datetime.now(timezone.utc)
    return SyncResult(
        fills_added=fills_added,
        orders_added=orders_added,
        activities_seen=len(activities),
        skipped=skipped,
    )


def sync_user_fills(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Sync every connected broker for one app user."""
    accts = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user_id)
    ).scalars())

    per_account: list[dict[str, Any]] = []
    total_fills = 0
    total_orders = 0
    for acct in accts:
        try:
            res = sync_account_fills(db, acct)
            per_account.append({
                "broker_account_id": str(acct.id),
                "broker": acct.broker.value,
                "fills_added": res.fills_added,
                "orders_added": res.orders_added,
                "skipped": res.skipped,
            })
            total_fills += res.fills_added
            total_orders += res.orders_added
        except Exception as exc:  # noqa: BLE001
            per_account.append({
                "broker_account_id": str(acct.id),
                "broker": acct.broker.value,
                "error": str(exc)[:300],
            })

    return {
        "per_account": per_account,
        "fills_added": total_fills,
        "orders_added": total_orders,
    }
