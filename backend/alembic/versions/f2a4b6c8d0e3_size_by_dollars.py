"""Size Discord entries by dollars

trader_settings.discord_size_mode ("contracts" | "dollars") and
trader_settings.discord_size_dollars — the amount per entry in dollars mode.

Revision ID: f2a4b6c8d0e3
Revises: e1f3a5b7c9d2
"""
import sqlalchemy as sa
from alembic import op

revision = "f2a4b6c8d0e3"
down_revision = "e1f3a5b7c9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_size_mode", sa.String(10), nullable=False, server_default="contracts"))
    op.add_column("trader_settings", sa.Column("discord_size_dollars", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_size_dollars")
    op.drop_column("trader_settings", "discord_size_mode")
