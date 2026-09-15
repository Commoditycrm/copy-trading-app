"""add Discord-specific sizing settings

Revision ID: e4a91c72b508
Revises: d8c3f6b21a95
Create Date: 2026-09-15 00:00:00.000000

Discord alerts rarely state a size, so the platform decides it. Kept separate
from the copy-trading multiplier on purpose: following a trader at 1x and sizing
Discord alerts at 3x are different decisions, and sharing one field would make
changing one silently change the other.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4a91c72b508"
down_revision: Union[str, None] = "d8c3f6b21a95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column("discord_quantity_multiplier", sa.Integer(), nullable=False,
                  server_default="1"),
    )
    op.add_column(
        "trader_settings",
        sa.Column("discord_max_per_contract", sa.Numeric(20, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_max_per_contract")
    op.drop_column("trader_settings", "discord_quantity_multiplier")
