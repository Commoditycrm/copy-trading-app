"""add active-window schedule to discord_alert_sources

Revision ID: d4a7c9e12b58
Revises: c8f1b3d29e04
Create Date: 2026-09-10 00:00:00.000000

Lets a trader restrict a source to an active window (e.g. US market hours) so a
browser session isn't held open around the clock. Defaults to "always", so every
existing source keeps its current behaviour.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "d4a7c9e12b58"
down_revision: Union[str, None] = "c8f1b3d29e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column("schedule_mode", sa.String(16), nullable=False, server_default="always"),
    )
    op.add_column("discord_alert_sources", sa.Column("schedule_start", sa.Time(), nullable=True))
    op.add_column("discord_alert_sources", sa.Column("schedule_end", sa.Time(), nullable=True))
    op.add_column(
        "discord_alert_sources", sa.Column("schedule_timezone", sa.String(64), nullable=True)
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("schedule_days", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    for col in (
        "schedule_days", "schedule_timezone", "schedule_end",
        "schedule_start", "schedule_mode",
    ):
        op.drop_column("discord_alert_sources", col)
