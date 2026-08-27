"""add active flag to sell_all_snapshots (one active snapshot per user)

A new Sell-All supersedes the prior snapshot (active=False); the Re-Enter card
shows only the active one. Older snapshots are retained so Re-Entry badges on
their orders keep resolving.

Revision ID: a7c1e9f2b4d8
Revises: f3a8d2c6b5e1
Create Date: 2026-08-27 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a7c1e9f2b4d8"
down_revision: Union[str, None] = "f3a8d2c6b5e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sell_all_snapshots",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_sell_all_snapshots_active", "sell_all_snapshots", ["active"])
    # Any pre-existing snapshots: keep only each user's newest as active.
    op.execute(
        """
        UPDATE sell_all_snapshots s SET active = false
        WHERE s.id NOT IN (
            SELECT DISTINCT ON (user_id) id FROM sell_all_snapshots
            ORDER BY user_id, created_at DESC
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_sell_all_snapshots_active", table_name="sell_all_snapshots")
    op.drop_column("sell_all_snapshots", "active")
