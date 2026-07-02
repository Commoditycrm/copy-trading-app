"""add follow_requests (subscriber → trader approval workflow)

Introduces an approval gate on following a trader. A subscriber creates a
request; the trader approves (grants permission) or rejects.

Grandfathering: every EXISTING direct-follow relationship
(subscriber_settings.following_trader_id set) is backfilled as an already-
``approved`` request so current subscribers aren't dropped or forced to
re-request when this ships.

Revision ID: b8d2f4a1c609
Revises: f1e2d3c4b5a6
Create Date: 2026-07-02 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b8d2f4a1c609"
down_revision: Union[str, None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    status_enum = sa.Enum(
        "pending", "approved", "rejected", name="follow_request_status",
    )
    op.create_table(
        "follow_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "trader_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", status_enum, nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("subscriber_id", "trader_id", name="uq_follow_request_pair"),
    )
    op.create_index("ix_follow_requests_subscriber_id", "follow_requests", ["subscriber_id"])
    op.create_index("ix_follow_requests_trader_id", "follow_requests", ["trader_id"])
    op.create_index("ix_follow_requests_status", "follow_requests", ["status"])

    # Grandfather current direct-follow relationships as approved so live
    # subscribers keep following without a new request. gen_random_uuid() is
    # built-in on PG13+ (our Neon instance is newer).
    op.execute(
        """
        INSERT INTO follow_requests
            (id, subscriber_id, trader_id, status, decided_at, created_at, updated_at)
        SELECT gen_random_uuid(), s.user_id, s.following_trader_id,
               'approved', now(), now(), now()
        FROM subscriber_settings s
        WHERE s.following_trader_id IS NOT NULL
        ON CONFLICT (subscriber_id, trader_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_follow_requests_status", table_name="follow_requests")
    op.drop_index("ix_follow_requests_trader_id", table_name="follow_requests")
    op.drop_index("ix_follow_requests_subscriber_id", table_name="follow_requests")
    op.drop_table("follow_requests")
    sa.Enum(name="follow_request_status").drop(op.get_bind(), checkfirst=True)
