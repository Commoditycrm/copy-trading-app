"""per-trim ladder settings — each rung gets its own gate and stop

The 1st trim's gate and stop were the only configurable ones; the 2nd's stop
was hardcoded to break-even and neither the 2nd nor the 3rd had a gate at all.

The existing discord_trim_profit_gate_pct / discord_trim_stop_pct ARE the first
trim's, so they are left alone and traders keep the values they already set.
The new columns default to 0, which reproduces the old behaviour exactly: a
gate of 0 is no minimum, and a stop 0% below entry is break-even.

Revision ID: a8d4f16b92c7
Revises: c5e9a73d41b8
"""
from alembic import op
import sqlalchemy as sa

revision = "a8d4f16b92c7"
# Chained AFTER subscriber max_per_order rather than beside it. Both were
# written against f3b7d21a6c94 on separate branches, which left two alembic
# heads once they met — and "alembic upgrade head" refuses to run at all
# with more than one. c5e9a73d41b8 is already the head on this PR's base
# branch, so this one goes on top of it and the chain stays linear.
down_revision = "c5e9a73d41b8"
branch_labels = None
depends_on = None

_COLS = (
    "discord_trim2_profit_gate_pct",
    "discord_trim2_stop_pct",
    "discord_trim3_profit_gate_pct",
    "discord_trim3_stop_pct",
)


def upgrade() -> None:
    for name in _COLS:
        op.add_column(
            "trader_settings",
            sa.Column(
                name, sa.Numeric(9, 4),
                nullable=False, server_default="0",
            ),
        )


def downgrade() -> None:
    for name in _COLS:
        op.drop_column("trader_settings", name)
