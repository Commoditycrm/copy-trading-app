"""discord_position_guards.entry_order_id — link a guard to the BUY that opened it

The ladder measures every level off ``entry_price``, which was recorded at
PLACEMENT as the alert's limit. A limit buy normally fills at or better than
that, so the reference was pessimistic but safe -- until the +10% entry reprice
landed, which can fill ABOVE the original limit and leaves the guard measuring
from a price the trader never paid.

Linking the opening order lets the guard adopt that order's real fill price,
without guessing which of several orders on the same contract opened it.

Revision ID: e7c4b2a91d38
Revises: b6f1d3a9c72e
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "e7c4b2a91d38"
down_revision = "b6f1d3a9c72e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discord_position_guards",
        sa.Column("entry_order_id", UUID(as_uuid=True), nullable=True),
    )
    # SET NULL, not CASCADE: losing the order row must not delete a guard that
    # is still protecting a live position.
    op.create_foreign_key(
        "fk_discord_guard_entry_order",
        "discord_position_guards", "orders",
        ["entry_order_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_discord_guard_entry_order", "discord_position_guards", type_="foreignkey"
    )
    op.drop_column("discord_position_guards", "entry_order_id")
