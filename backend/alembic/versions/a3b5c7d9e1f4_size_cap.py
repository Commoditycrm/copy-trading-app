"""One sizing cap at a time

trader_settings.discord_size_cap — "per_contract" | "per_order" | "none"; NULL
means never chosen (whichever cap has a value applies, max per contract first).

Revision ID: a3b5c7d9e1f4
Revises: f2a4b6c8d0e3
"""
import sqlalchemy as sa
from alembic import op

revision = "a3b5c7d9e1f4"
down_revision = "f2a4b6c8d0e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column("discord_size_cap", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_size_cap")
