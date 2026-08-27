"""add sell_all_snapshots (Sell-All snapshot + re-entry)

Records the positions a user held right before a Sell-All so they can re-enter
the same set later (at market, or a % below the exit price).

Revision ID: f3a8d2c6b5e1
Revises: e2b7c4f1a9d3
Create Date: 2026-08-27 08:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "f3a8d2c6b5e1"
down_revision: Union[str, None] = "e2b7c4f1a9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sell_all_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("positions", JSONB, nullable=False),
    )
    op.create_index("ix_sell_all_snapshots_user_id", "sell_all_snapshots", ["user_id"])
    op.create_index("ix_sell_all_snapshots_created_at", "sell_all_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_sell_all_snapshots_created_at", table_name="sell_all_snapshots")
    op.drop_index("ix_sell_all_snapshots_user_id", table_name="sell_all_snapshots")
    op.drop_table("sell_all_snapshots")
