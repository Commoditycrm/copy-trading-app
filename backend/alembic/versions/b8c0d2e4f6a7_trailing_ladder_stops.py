"""Trailing stops on the exit ladder

trader_settings.discord_stop_trails — which ladder stops trail (On Fill and
each trim) instead of sitting at a fixed level.
discord_position_guards.stop_trail_pct / stop_peak — a holding's trailing stop:
its give-back and the high it is measured from.

Revision ID: b8c0d2e4f6a7
Revises: a7b9c1d3e5f4
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8c0d2e4f6a7"
down_revision = "a7b9c1d3e5f4"
branch_labels = None
depends_on = None

_G = "discord_position_guards"


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_stop_trails", postgresql.JSONB(), nullable=True))
    op.add_column(_G, sa.Column("stop_trail_pct", sa.Numeric(9, 4), nullable=True))
    op.add_column(_G, sa.Column("stop_peak", sa.Numeric(18, 4), nullable=True))


def downgrade() -> None:
    op.drop_column(_G, "stop_peak")
    op.drop_column(_G, "stop_trail_pct")
    op.drop_column("trader_settings", "discord_stop_trails")
