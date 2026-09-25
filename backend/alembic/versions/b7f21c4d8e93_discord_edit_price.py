"""orders.discord_edit_price — the price an edited alert is still waiting to apply

An alert edited in place ("$SPY 770 CALL 0DTE @0.20" becomes "@0.15") is a
correction to the order it already placed. The broker frequently cannot apply it
at the moment it arrives: pre-market an Alpaca option rests in `accepted`
(received, not yet routed) and will not take a PATCH until options start routing
at 09:30. The wanted price is held here and retried, rather than dropped.

Deliberately NOT inferred from "the limit differs from the alert" — our own +10%
reprice moves the limit AWAY from the alert price, so that test is true in both
cases and means opposite things.

Revision ID: b7f21c4d8e93
Revises: a8d4f16b92c7
"""
import sqlalchemy as sa
from alembic import op

revision = "b7f21c4d8e93"
down_revision = "a8d4f16b92c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("discord_edit_price", sa.Numeric(18, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "discord_edit_price")
