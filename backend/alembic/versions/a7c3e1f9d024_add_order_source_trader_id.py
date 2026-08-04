"""add orders.source_trader_id (multi-trader order attribution) + merge heads

Adds a denormalized ``source_trader_id`` to ``orders`` so every order can be
attributed to the trader who originated its signal, without walking
``parent_order_id → parent.user_id``. This is what multi-trader following needs:
a subscriber who follows many traders receives mirrors from each, and each mirror
now carries the originating trader's id directly.

Also MERGES the two open heads:
  * ``5f7a9c1e2b34`` — add subscriber_follows (multi-trader following)
  * ``a2c7f4e91b83`` — add max_account_usd_per_day
into a single head.

Backfill:
  * mirrors (parent_order_id set) → the parent order's owner (the trader)
  * everything else               → the order's own user_id (self)

Revision ID: a7c3e1f9d024
Revises: 5f7a9c1e2b34, a2c7f4e91b83
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a7c3e1f9d024"
down_revision = ("5f7a9c1e2b34", "a2c7f4e91b83")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "source_trader_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_orders_source_trader_id", "orders", ["source_trader_id"]
    )

    # Backfill: mirrors inherit their trader (parent owner); all other orders
    # (trader roots, subscribers' manual orders) are attributed to themselves.
    op.execute(
        """
        UPDATE orders o
        SET source_trader_id = p.user_id
        FROM orders p
        WHERE o.parent_order_id = p.id
        """
    )
    op.execute(
        """
        UPDATE orders
        SET source_trader_id = user_id
        WHERE source_trader_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_orders_source_trader_id", table_name="orders")
    op.drop_column("orders", "source_trader_id")
