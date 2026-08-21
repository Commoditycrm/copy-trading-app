"""add order + P&L soft-delete (admin hide user history)

Admin can hide a user's order history + P&L durably. A hard DELETE didn't stick —
the trade_listener re-pulled the same orders from the broker and recreated them
(prod KPneverquits 2026-08-21: 30 of 47 deleted orders reappeared). Soft-delete
keeps the row so the sync's dedup finds it and won't recreate it, and the flag
is preserved on re-import.

  * orders.hidden_at / hidden_by  — soft-delete the order (hidden from history +
    db_realized P&L; sync won't un-hide it)
  * daily_realized_pnl_snapshots.hidden — suppress a broker-fed P&L day
    (Alpaca/SnapTrade P&L comes from the broker, not our orders, so hiding
    orders alone can't clear it)

Revision ID: b1d7f3e9a2c4
Revises: c8e2f1a4d6b9
Create Date: 2026-08-21 06:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b1d7f3e9a2c4"
down_revision: Union[str, None] = "c8e2f1a4d6b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("hidden_by", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_orders_user_hidden", "orders", ["user_id", "hidden_at"])
    op.add_column(
        "daily_realized_pnl_snapshots",
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("daily_realized_pnl_snapshots", "hidden")
    op.drop_index("ix_orders_user_hidden", table_name="orders")
    op.drop_column("orders", "hidden_by")
    op.drop_column("orders", "hidden_at")
