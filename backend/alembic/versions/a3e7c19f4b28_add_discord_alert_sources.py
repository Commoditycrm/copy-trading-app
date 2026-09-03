"""add discord_alert_sources (inbound Discord alert-copying, step 1)

Revision ID: a3e7c19f4b28
Revises: f2b8c1d4e7a9
Create Date: 2026-09-02 00:00:00.000000

A trader-connected Discord channel that Kopyaa reads trade alerts FROM. Separate
from the outbound webhook broadcast (traders_settings.discord_webhook_url).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "a3e7c19f4b28"
down_revision: Union[str, None] = "f2b8c1d4e7a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discord_alert_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.String(40), nullable=True),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("channel_name", sa.String(200), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_discord_alert_sources_user_id", "discord_alert_sources", ["user_id"])
    op.create_index("ix_discord_alert_sources_channel_id", "discord_alert_sources", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_discord_alert_sources_channel_id", table_name="discord_alert_sources")
    op.drop_index("ix_discord_alert_sources_user_id", table_name="discord_alert_sources")
    op.drop_table("discord_alert_sources")
