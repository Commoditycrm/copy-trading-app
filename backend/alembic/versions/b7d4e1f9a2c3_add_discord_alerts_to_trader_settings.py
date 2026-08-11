"""add_discord_alerts_to_trader_settings

Revision ID: b7d4e1f9a2c3
Revises: a3f9d1c7e2b8
Create Date: 2026-08-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7d4e1f9a2c3'
down_revision: Union[str, None] = 'a3f9d1c7e2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trader_settings', sa.Column('discord_webhook_url', sa.String(length=500), nullable=True))
    op.add_column('trader_settings', sa.Column('discord_alerts_enabled', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('trader_settings', 'discord_alerts_enabled')
    op.drop_column('trader_settings', 'discord_webhook_url')
