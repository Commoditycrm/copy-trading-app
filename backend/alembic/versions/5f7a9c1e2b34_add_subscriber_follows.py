"""add subscriber_follows (multi-trader following) + merge heads

Creates the ``subscriber_follows`` join table so a subscriber can follow MANY
traders at once. Copy settings stay global on ``subscriber_settings`` — this
table only records relationships. Backfills one row per existing
``following_trader_id`` so current subscribers behave identically (each ends up
following exactly the one trader they follow today).

Also MERGES the two open migration heads (copy_trader_bracket + snapshot_pct)
into a single head.

Revision ID: 5f7a9c1e2b34
Revises: d4e5f6a7b8c9, d5c6b7a8e9f0
Create Date: 2026-07-31

NOTE: this migration originally shipped with revision id ``f1a2b3c4d5e6``, which
COLLIDED with the pre-existing ``add_admin_role`` migration (same id). The
duplicate made the whole Alembic graph unresolvable ("present more than once" /
"Cycle is detected"). It was renamed to ``5f7a9c1e2b34`` (a fresh, unique id) to
repair the graph. Nothing chained onto the old id here (``add_webull`` chains
onto ``add_admin_role``'s ``f1a2b3c4d5e6``), so the rename is safe.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "5f7a9c1e2b34"
down_revision = ("d4e5f6a7b8c9", "d5c6b7a8e9f0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_follows",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "trader_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("subscriber_id", "trader_id", name="uq_subscriber_follow_pair"),
    )
    op.create_index("ix_subscriber_follows_subscriber_id", "subscriber_follows", ["subscriber_id"])
    op.create_index("ix_subscriber_follows_trader_id", "subscriber_follows", ["trader_id"])

    # Backfill: every current single-follow becomes one row → identical behavior.
    op.execute(
        """
        INSERT INTO subscriber_follows (id, subscriber_id, trader_id, created_at, updated_at)
        SELECT gen_random_uuid(), user_id, following_trader_id, now(), now()
        FROM subscriber_settings
        WHERE following_trader_id IS NOT NULL
        ON CONFLICT (subscriber_id, trader_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_subscriber_follows_trader_id", table_name="subscriber_follows")
    op.drop_index("ix_subscriber_follows_subscriber_id", table_name="subscriber_follows")
    op.drop_table("subscriber_follows")
