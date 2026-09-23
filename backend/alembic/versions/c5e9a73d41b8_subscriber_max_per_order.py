"""subscriber_settings.max_per_order — cap the whole mirror's value

max_per_contract caps what ONE contract may cost. This caps the mirror's total,
which a per-contract cap cannot express: ten contracts at $50 is a cheap
contract and a $500 order. Independent of it, and applies to stock mirrors too.

Revision ID: c5e9a73d41b8
Revises: f3b7d21a6c94
"""
from alembic import op
import sqlalchemy as sa

revision = "c5e9a73d41b8"
down_revision = "f3b7d21a6c94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no default: NULL is "no cap", so every existing subscriber keeps
    # exactly the behaviour they have today.
    op.add_column(
        "subscriber_settings",
        sa.Column("max_per_order", sa.Numeric(20, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "max_per_order")
