"""trim stops become a SIGNED offset from entry

Before: the value was a DISTANCE BELOW entry, so 25 meant entry x 0.75 and the
highest a stop could ever sit was break-even (0). There was no way to express
"move the stop to +10% and lock in profit".

After: the value is the RETURN the stop sits at. -25 is entry x 0.75, 0 is
break-even, +10 is entry x 1.10.

Every stored value is therefore NEGATED, so a ladder that read 25 now reads -25
and its stop sits exactly where it always did. 0 negates to 0, so a break-even
rung is untouched. Nobody's live protection moves.

Revision ID: e4c9d2a6b183
Revises: d5b62e91af07
"""
import sqlalchemy as sa
from alembic import op

revision = "e4c9d2a6b183"
down_revision = "d5b62e91af07"
branch_labels = None
depends_on = None

_STOPS = ("discord_trim_stop_pct", "discord_trim2_stop_pct", "discord_trim3_stop_pct")


def _flip() -> None:
    # -0 is 0 for numeric, so a break-even rung stays break-even.
    op.execute(
        "UPDATE trader_settings SET "
        + ", ".join(f"{c} = -{c}" for c in _STOPS)
    )


def upgrade() -> None:
    _flip()
    # The 1st trim's default follows the same flip: 25% below entry is now -25.
    op.alter_column(
        "trader_settings", "discord_trim_stop_pct",
        existing_type=sa.Numeric(9, 4), existing_nullable=False,
        server_default=sa.text("-25"),
    )


def downgrade() -> None:
    _flip()
    op.alter_column(
        "trader_settings", "discord_trim_stop_pct",
        existing_type=sa.Numeric(9, 4), existing_nullable=False,
        server_default=sa.text("25"),
    )
