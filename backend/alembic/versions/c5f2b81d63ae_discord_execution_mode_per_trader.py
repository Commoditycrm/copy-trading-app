"""move Discord execution mode from each channel to one per-trader setting

Revision ID: c5f2b81d63ae
Revises: b93d1e7a04c2
Create Date: 2026-09-11 00:00:00.000000

The mode expresses how much a trader trusts automation in general, not something
that differs per feed — and splitting it per channel made it easy to leave one
on auto by accident.

Migration keeps intent: a trader with ANY channel on auto keeps auto.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5f2b81d63ae"
down_revision: Union[str, None] = "b93d1e7a04c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column("discord_execution_mode", sa.String(10), nullable=False,
                  server_default="manual"),
    )
    op.execute(
        """
        UPDATE trader_settings ts
           SET discord_execution_mode = 'auto'
         WHERE EXISTS (
            SELECT 1 FROM discord_alert_sources s
             WHERE s.user_id = ts.user_id AND s.execution_mode = 'auto'
         )
        """
    )
    op.drop_column("discord_alert_sources", "execution_mode")


def downgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column("execution_mode", sa.String(10), nullable=False, server_default="manual"),
    )
    op.execute(
        """
        UPDATE discord_alert_sources s
           SET execution_mode = ts.discord_execution_mode
          FROM trader_settings ts
         WHERE ts.user_id = s.user_id
        """
    )
    op.drop_column("trader_settings", "discord_execution_mode")
