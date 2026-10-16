"""discord_alert_sources: per-channel alert handling + entry order type

- use_account_settings (default TRUE): the channel follows the account-wide
  Discord settings, exactly as every channel did before this migration.
- channel_settings (JSON): the channel's own values once it stops following the
  account (a copy of the account's, then edited).
- entry_order_type ("limit" | "market", default "limit"): how entries from this
  channel are sent. Exits are unchanged (they follow the exit ladder).

Revision ID: a3c5e7f9b1d2
Revises: f2b4d6e8a0c3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a3c5e7f9b1d2"
down_revision = "f2b4d6e8a0c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discord_alert_sources", sa.Column(
        "use_account_settings", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("discord_alert_sources", sa.Column(
        "channel_settings", JSONB(), nullable=False, server_default="{}"))
    op.add_column("discord_alert_sources", sa.Column(
        "entry_order_type", sa.String(10), nullable=False, server_default="limit"))


def downgrade() -> None:
    op.drop_column("discord_alert_sources", "entry_order_type")
    op.drop_column("discord_alert_sources", "channel_settings")
    op.drop_column("discord_alert_sources", "use_account_settings")
