"""add the Discord live-trading switch

Revision ID: d8c3f6b21a95
Revises: c5f2b81d63ae
Create Date: 2026-09-15 00:00:00.000000

Defaults to FALSE (paper). Approved alerts run the whole pipeline — validation,
contract resolution, sizing, pricing — and record what WOULD have been placed,
without sending anything to a broker. Live trading is an explicit, deliberate
switch, never something a trader arrives at by accident.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8c3f6b21a95"
down_revision: Union[str, None] = "c5f2b81d63ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column("discord_live_trading", sa.Boolean(), nullable=False,
                  server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_live_trading")
