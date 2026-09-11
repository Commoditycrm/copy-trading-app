"""add orders.trail_down_percent (managed "trail down" buy limit)

Revision ID: c9d0e1f2a3b4
Revises: b8d1f4a2c6e9
Create Date: 2026-09-11 00:00:00.000000

Adds a nullable ``trail_down_percent`` on orders. It's set only on a "Trail
down" re-entry — a managed BUY LIMIT whose limit trail_down_monitor re-prices
this % below the falling live price (ratchets DOWN only). NULL on every other
order.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8d1f4a2c6e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("trail_down_percent", sa.Numeric(9, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "trail_down_percent")
