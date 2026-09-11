"""add orders.trail_down_percent (managed "trail down" buy limit) + merge heads

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9, d5c6b7a8e9f0
Create Date: 2026-09-11 00:00:00.000000

Adds a nullable ``trail_down_percent`` on orders. It's set only on a "Trail
down" re-entry — a managed BUY LIMIT whose limit trail_down_monitor re-prices
this % below the falling live price (ratchets DOWN only). NULL on every other
order. Also unifies the two open migration heads so deploy has a single head.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = ("d4e5f6a7b8c9", "d5c6b7a8e9f0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("trail_down_percent", sa.Numeric(9, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "trail_down_percent")
