"""discord alert sources: bot token -> browser session ingestion

Revision ID: b6e4a2c81d37
Revises: c4f1a9d7e230
Create Date: 2026-09-08 00:00:00.000000

Step 1 of inbound Discord alert-copying stored the trader's own BOT token and
read a Channel-Following mirror channel. That model is replaced by monitoring
Discord Web as the trader's own authenticated account, so the bot token column
goes away and a Fernet-encrypted Playwright session takes its place, alongside
the listener's connection-liveness columns.

Destructive on the credential column by design: a bot token is useless to the
browser listener, and keeping it would leave a live secret at rest for a code
path that no longer exists. Sources are re-authorised with a one-time headed
Discord login, so every existing row correctly lands back on status
'needs_login'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6e4a2c81d37"
down_revision: Union[str, None] = "c4f1a9d7e230"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column("guild_name", sa.String(200), nullable=True),
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("encrypted_session", sa.Text(), nullable=True),
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("session_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("last_seen_message_id", sa.String(40), nullable=True),
    )

    # The bot token is meaningless to the browser listener. Drop it rather than
    # leave an orphaned secret encrypted at rest.
    op.drop_column("discord_alert_sources", "encrypted_credentials")

    # Every pre-existing row was connected via a bot and now has no browser
    # session, so it must re-authorise before the listener will open it.
    op.execute("UPDATE discord_alert_sources SET status = 'needs_login'")
    op.alter_column(
        "discord_alert_sources",
        "status",
        server_default="needs_login",
        existing_type=sa.String(20),
        existing_nullable=False,
    )

    # Guard against one trader watching the same channel twice — duplicate
    # sources would each ingest the same Discord message under a different
    # source id, defeating the per-source idempotency key downstream.
    # De-duplicate any existing offenders (keeping the oldest) before the
    # constraint goes on, or the migration would fail on live data.
    op.execute(
        """
        DELETE FROM discord_alert_sources a
        USING discord_alert_sources b
        WHERE a.user_id = b.user_id
          AND a.channel_id = b.channel_id
          AND a.created_at > b.created_at
        """
    )
    op.create_unique_constraint(
        "uq_discord_source_user_channel",
        "discord_alert_sources",
        ["user_id", "channel_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_discord_source_user_channel", "discord_alert_sources", type_="unique"
    )
    # Restored NOT NULL with an empty default: the original bot tokens are gone
    # for good, so a downgraded row can only be reconnected, never resumed.
    op.add_column(
        "discord_alert_sources",
        sa.Column("encrypted_credentials", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column(
        "discord_alert_sources",
        "status",
        server_default="pending",
        existing_type=sa.String(20),
        existing_nullable=False,
    )
    for col in (
        "last_seen_message_id",
        "last_message_at",
        "last_heartbeat_at",
        "session_captured_at",
        "encrypted_session",
        "guild_name",
    ):
        op.drop_column("discord_alert_sources", col)
