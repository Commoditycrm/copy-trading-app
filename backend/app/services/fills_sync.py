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

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.models.broker_account import BrokerAccount
from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services.crypto import decrypt_json


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
    for order in open_orders:
        try:
            res = adapter.get_order(order.broker_order_id)
        except Exception:  # noqa: BLE001
            # Order may have been cancelled at the broker side or the id is
            # stale — don't fail the whole sync over one bad lookup.
            continue
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
        if changed:
            refreshed += 1
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
        # we already placed, update THAT order in place. Otherwise (external
        # trades placed in Alpaca's UI directly, or pre-existing fills) create
        # a synthetic order so the activity still surfaces in the UI.
        broker_oid = str(_attr(entry, "order_id") or "") or None
        order: Order | None = None
        if broker_oid:
            order = db.execute(
                select(Order).where(
                    Order.broker_account_id == acct.id,
                    Order.broker_order_id == broker_oid,
                )
            ).scalar_one_or_none()

        if order is None:
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
            # Accumulate qty + recompute volume-weighted avg fill price across
            # all fills we've seen on this order so far.
            prev_qty = _dec(order.filled_quantity)
            prev_avg = _dec(order.filled_avg_price)
            new_qty = prev_qty + units
            order.filled_quantity = new_qty
            order.filled_avg_price = (
                (prev_qty * prev_avg + units * price) / new_qty
                if new_qty > 0 else price
            )
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
