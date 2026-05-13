"""Sync filled trades from SnapTrade into our local fills + orders tables.

For each BUY/SELL activity SnapTrade reports for a broker account, we:
  1. Dedup by activity.id (mapped to Fill.broker_fill_id) — already-synced
     activities are skipped.
  2. Upsert a synthetic Order row (status=FILLED) — activities don't carry a
     broker_order_id we can match to our existing Orders, so we treat each
     activity as a self-contained order. This means fills from external trades
     (placed in Alpaca's UI directly) also surface in our Trades / Calendar.
  3. Insert a Fill row attached to that Order.

Stocks only for now — options activities are skipped (we can't trade them
through SnapTrade on the test tier anyway).

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

from app.models.broker_account import BrokerAccount
from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services import snaptrade as st


@dataclass
class SyncResult:
    fills_added: int
    orders_added: int
    activities_seen: int
    skipped: int   # non-buy/sell or already-synced


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # SnapTrade returns ISO8601 with trailing 'Z'. fromisoformat in 3.11+ handles it.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _dec(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def sync_account_fills(db: Session, acct: BrokerAccount, user_secret: str) -> SyncResult:
    """Pull activities for `acct` from SnapTrade and upsert fills locally.
    Idempotent — re-runs are safe."""
    activities = st.list_account_activities(acct.user_id, user_secret, acct.snaptrade_account_id)

    fills_added = 0
    orders_added = 0
    skipped = 0

    # Pre-fetch existing fills for this account (by broker_fill_id) so we don't
    # round-trip the DB once per activity.
    existing_fill_ids: set[str] = set(
        r[0] for r in db.execute(
            select(Fill.broker_fill_id)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.broker_account_id == acct.id, Fill.broker_fill_id.isnot(None))
        ).all()
    )

    for entry in activities:
        if not isinstance(entry, dict):
            skipped += 1
            continue

        # Stocks only. SnapTrade activity `type` for fills is "BUY" or "SELL".
        # Other types (DIVIDEND, FEE, DEPOSIT, etc.) are skipped.
        atype = (entry.get("type") or "").upper()
        if atype not in ("BUY", "SELL"):
            skipped += 1
            continue

        # Skip option activities — different lifecycle, not in scope.
        if entry.get("option_symbol") is not None:
            skipped += 1
            continue

        activity_id = entry.get("id")
        if not activity_id or str(activity_id) in existing_fill_ids:
            skipped += 1
            continue

        sym_obj = entry.get("symbol") or {}
        ticker = sym_obj.get("symbol") or sym_obj.get("raw_symbol")
        if not ticker:
            skipped += 1
            continue

        units = _dec(entry.get("units"))
        price = _dec(entry.get("price"))
        fee = _dec(entry.get("fee"))
        if units <= 0 or price <= 0:
            skipped += 1
            continue

        trade_at = _parse_dt(entry.get("trade_date")) or datetime.now(timezone.utc)
        side = OrderSide.BUY if atype == "BUY" else OrderSide.SELL

        # Synthetic Order. Use activity_id as broker_order_id for cross-run dedup
        # and so the FIFO calculator has the side/instrument metadata it needs.
        order = Order(
            user_id=acct.user_id,
            broker_account_id=acct.id,
            instrument_type=InstrumentType.STOCK,
            symbol=ticker.upper(),
            side=side,
            order_type=OrderType.MARKET,
            quantity=units,
            status=OrderStatus.FILLED,
            broker_order_id=str(activity_id),
            filled_quantity=units,
            filled_avg_price=price,
            submitted_at=trade_at,
            closed_at=trade_at,
        )
        db.add(order)
        db.flush()
        orders_added += 1

        fill = Fill(
            order_id=order.id,
            quantity=units,
            price=price,
            fee=fee,
            filled_at=trade_at,
            broker_fill_id=str(activity_id),
        )
        db.add(fill)
        fills_added += 1
        existing_fill_ids.add(str(activity_id))

    acct.last_activity_sync_at = datetime.now(timezone.utc)

    return SyncResult(
        fills_added=fills_added,
        orders_added=orders_added,
        activities_seen=len(activities),
        skipped=skipped,
    )


def sync_user_fills(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Sync every connected broker for one app user. Best-effort: per-broker
    failures don't abort the whole sync."""
    from app.models.user import User
    user = db.get(User, user_id)
    if not user or not user.encrypted_snaptrade_user_secret:
        return {"per_account": [], "fills_added": 0, "orders_added": 0}

    secret = st.decrypt_secret(user.encrypted_snaptrade_user_secret)

    accts = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user_id)
    ).scalars())

    per_account: list[dict[str, Any]] = []
    total_fills = 0
    total_orders = 0
    for acct in accts:
        try:
            res = sync_account_fills(db, acct, secret)
            per_account.append({
                "broker_account_id": str(acct.id),
                "broker": acct.broker,
                "fills_added": res.fills_added,
                "orders_added": res.orders_added,
                "skipped": res.skipped,
            })
            total_fills += res.fills_added
            total_orders += res.orders_added
        except Exception as exc:  # noqa: BLE001
            per_account.append({
                "broker_account_id": str(acct.id),
                "broker": acct.broker,
                "error": str(exc)[:300],
            })

    return {
        "per_account": per_account,
        "fills_added": total_fills,
        "orders_added": total_orders,
    }
