"""Dollar-target sizing on subscriber_settings

Adds the opt-in per-trade dollar budget sizing mode. Additive and backward
compatible: existing rows default to sizing_mode="multiplier" (no behaviour
change) and risk_per_trade_usd NULL.

Revision ID: e7f1a2b3c4d5
Revises: b4c6d8e0f2a5
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f1a2b3c4d5"
down_revision = "b4c6d8e0f2a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "sizing_mode",
            sa.String(length=16),
            nullable=False,
            server_default="multiplier",
        ),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column("risk_per_trade_usd", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "risk_per_trade_usd")
    op.drop_column("subscriber_settings", "sizing_mode")
