"""trader_settings: how much of the remaining position each trim sells

The ladder used to hardcode this — half on the first two rungs, everything on
the third. The defaults here are exactly that (50 / 50 / 100), so an existing
ladder keeps its shape; setting them is what lets a trader say "take 30% first".

Measured against what is STILL HELD, not the original position, which is what
makes the rungs compose.

Revision ID: d5b62e91af07
Revises: c3a81f5e27d4
"""
import sqlalchemy as sa
from alembic import op

revision = "d5b62e91af07"
down_revision = "c3a81f5e27d4"
branch_labels = None
depends_on = None

_COLS = (("discord_trim_qty_pct", "50"),
         ("discord_trim2_qty_pct", "50"),
         ("discord_trim3_qty_pct", "100"))


def upgrade() -> None:
    for name, default in _COLS:
        op.add_column(
            "trader_settings",
            sa.Column(name, sa.Numeric(9, 4), nullable=False,
                      server_default=sa.text(default)),
        )


def downgrade() -> None:
    for name, _ in _COLS:
        op.drop_column("trader_settings", name)
