"""move the Discord session from each source onto a shared account

Revision ID: a7e3f92c1b04
Revises: f1c8a05de234
Create Date: 2026-09-11 00:00:00.000000

A Discord session authenticates an ACCOUNT, not a channel. Holding it on each
source meant a fresh sign-in for every channel — and a separate Discord session
opened for each one, even when they were all the same account.

Existing sources are migrated: each user's connected sources are folded into one
account carrying the most recently captured session, so nobody has to reconnect.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "a7e3f92c1b04"
down_revision: Union[str, None] = "f1c8a05de234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discord_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False, server_default="Discord account"),
        sa.Column("discord_username", sa.String(120), nullable=True),
        sa.Column("encrypted_session", sa.Text(), nullable=True),
        sa.Column("session_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="needs_login"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_discord_accounts_user_id", "discord_accounts", ["user_id"])

    op.add_column(
        "discord_alert_sources",
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("discord_accounts.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(
        "ix_discord_alert_sources_account_id", "discord_alert_sources", ["account_id"]
    )

    # One account per user, seeded with their newest stored session, and every
    # source attached to it — so an existing setup keeps working untouched.
    op.execute(
        """
        INSERT INTO discord_accounts
            (id, user_id, label, encrypted_session, session_captured_at, status,
             created_at, updated_at)
        SELECT
            gen_random_uuid(),
            s.user_id,
            'Discord account',
            (SELECT s2.encrypted_session FROM discord_alert_sources s2
              WHERE s2.user_id = s.user_id AND s2.encrypted_session IS NOT NULL
              ORDER BY s2.session_captured_at DESC NULLS LAST LIMIT 1),
            (SELECT s2.session_captured_at FROM discord_alert_sources s2
              WHERE s2.user_id = s.user_id AND s2.encrypted_session IS NOT NULL
              ORDER BY s2.session_captured_at DESC NULLS LAST LIMIT 1),
            CASE WHEN EXISTS (
                SELECT 1 FROM discord_alert_sources s3
                 WHERE s3.user_id = s.user_id AND s3.encrypted_session IS NOT NULL
            ) THEN 'connected' ELSE 'needs_login' END,
            now(), now()
        FROM discord_alert_sources s
        GROUP BY s.user_id
        """
    )
    op.execute(
        """
        UPDATE discord_alert_sources s
           SET account_id = a.id
          FROM discord_accounts a
         WHERE a.user_id = s.user_id
        """
    )

    op.drop_column("discord_alert_sources", "encrypted_session")
    op.drop_column("discord_alert_sources", "session_captured_at")


def downgrade() -> None:
    op.add_column(
        "discord_alert_sources", sa.Column("encrypted_session", sa.Text(), nullable=True)
    )
    op.add_column(
        "discord_alert_sources",
        sa.Column("session_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Push the account's session back onto each of its sources.
    op.execute(
        """
        UPDATE discord_alert_sources s
           SET encrypted_session = a.encrypted_session,
               session_captured_at = a.session_captured_at
          FROM discord_accounts a
         WHERE a.id = s.account_id
        """
    )
    op.drop_index("ix_discord_alert_sources_account_id", table_name="discord_alert_sources")
    op.drop_column("discord_alert_sources", "account_id")
    op.drop_index("ix_discord_accounts_user_id", table_name="discord_accounts")
    op.drop_table("discord_accounts")
