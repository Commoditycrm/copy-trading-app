"""discord_alert_sources.parent_source_id: a subscriber's copy of a trader channel

A subscriber following a Discord trader gets one source per trader channel,
owned by the subscriber and linked here to the trader's. Alerts the trader's
channel receives are ingested into each copy and executed on the subscriber's
own settings — independent of the trader's order. Deleting the trader's
channel DETACHES the copies (SET NULL) — the subscriber keeps their history.

Revision ID: f2b4d6e8a0c3
Revises: e1a3c5d7f9b2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "f2b4d6e8a0c3"
down_revision = "e1a3c5d7f9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column(
            "parent_source_id", UUID(as_uuid=True),
            sa.ForeignKey("discord_alert_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_discord_alert_sources_parent_source_id",
        "discord_alert_sources", ["parent_source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_discord_alert_sources_parent_source_id", "discord_alert_sources")
    op.drop_column("discord_alert_sources", "parent_source_id")
