"""Pause ALL channels

discord_alert_sources.paused_by_pause_all — the channels "Pause ALL channels"
switched off, so "Resume ALL channels" turns back on exactly those.

Revision ID: c9d1e3f5a7b9
Revises: b8c0d2e4f6a7
"""
import sqlalchemy as sa
from alembic import op

revision = "c9d1e3f5a7b9"
down_revision = "b8c0d2e4f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discord_alert_sources", sa.Column(
        "paused_by_pause_all", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("discord_alert_sources", "paused_by_pause_all")
