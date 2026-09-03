"""add sell_all_access to users

Per-user allow-list gate for the Sell-All / Snapshot / Re-entry suite.
Off by default; an admin toggles it per trader.

Revision ID: b8d1f4a2c6e9
Revises: c1a9e3f7b2d5
Create Date: 2026-09-03 13:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d1f4a2c6e9"
down_revision: Union[str, None] = "c1a9e3f7b2d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("sell_all_access", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "sell_all_access")
