"""trader_settings.discord_max_per_order — cap the whole order's value

discord_max_per_contract caps what a SINGLE contract may cost. This caps the
order's total, which is a different question: ten contracts at $50 each is a
cheap contract and a $500 order. Both are optional and independent.

Revision ID: f3b7d21a6c94
Revises: e7c4b2a91d38
"""
from alembic import op
import sqlalchemy as sa

revision = "f3b7d21a6c94"
down_revision = "e7c4b2a91d38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: NULL means "no ceiling", so existing traders
    # keep exactly the behaviour they have today.
    op.add_column(
        "trader_settings",
        sa.Column("discord_max_per_order", sa.Numeric(20, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_max_per_order")
