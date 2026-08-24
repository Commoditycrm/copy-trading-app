"""Single source of truth for the admin soft-delete ("Hide P&L") read filter.

Rule: EVERY user- or admin-facing READ of a user's orders or P&L must exclude
hidden rows. Route those reads through these helpers so a new read path can't
silently leak hidden data — change the predicate here and it changes everywhere.

DO NOT apply these on the sync / listener / write-by-id paths. Those MUST still
see hidden rows: the broker re-import looks an order up by broker_order_id and
updates it in place; if that lookup couldn't see the hidden row it would insert
a duplicate and effectively un-hide the data. Keeping the row visible to the
sync (but hidden to reads) is exactly what makes the hide durable.
"""
from __future__ import annotations

from sqlalchemy import ColumnElement, Select

from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.models.order import Order


def order_is_visible() -> "ColumnElement[bool]":
    """WHERE-clause predicate: this order is not admin-hidden."""
    return Order.hidden_at.is_(None)


def snapshot_is_visible() -> "ColumnElement[bool]":
    """WHERE-clause predicate: this P&L snapshot day is not admin-hidden."""
    return DailyRealizedPnlSnapshot.hidden.is_(False)


def visible_orders(q: Select) -> Select:
    """Restrict a Select over orders to non-hidden rows."""
    return q.where(order_is_visible())


def visible_snapshots(q: Select) -> Select:
    """Restrict a Select over daily P&L snapshots to non-hidden rows."""
    return q.where(snapshot_is_visible())
