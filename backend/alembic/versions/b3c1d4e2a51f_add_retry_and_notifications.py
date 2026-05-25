"""add retry policy + notifications

Adds:
  - retry_interval enum (never/1m/2m/3m/5m)
  - subscriber_settings.retry_interval_open
  - subscriber_settings.retry_interval_close
  - orders.retry_at (timestamp)
  - orders.retry_attempted (bool)
  - orders.is_closing (bool)
  - order_status enum gains RETRY_PENDING value
  - notifications table

Revision ID: b3c1d4e2a51f
Revises: e7a1d2c40f01
Create Date: 2026-05-23 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3c1d4e2a51f"
down_revision: Union[str, None] = "e7a1d2c40f01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add RETRY_PENDING to the existing order_status enum ─────────────
    # ALTER TYPE ... ADD VALUE can't run inside a transaction in Postgres
    # before v12. All currently-supported versions are 12+.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'retry_pending'"
        )

    # ── 2. Create the retry_interval enum (used by both new columns) ───────
    retry_interval_enum = sa.Enum(
        "never", "1m", "2m", "3m", "5m",
        name="retry_interval",
    )
    retry_interval_enum.create(op.get_bind(), checkfirst=True)

    # ── 3. subscriber_settings: 2 retry columns ────────────────────────────
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "retry_interval_open",
            sa.Enum(
                "never", "1m", "2m", "3m", "5m",
                name="retry_interval", create_type=False,
            ),
            nullable=False,
            server_default="never",
        ),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "retry_interval_close",
            sa.Enum(
                "never", "1m", "2m", "3m", "5m",
                name="retry_interval", create_type=False,
            ),
            nullable=False,
            server_default="never",
        ),
    )

    # ── 4. orders: retry_at, retry_attempted, is_closing ───────────────────
    op.add_column(
        "orders",
        sa.Column(
            "retry_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "retry_attempted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "is_closing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Index on retry_at for the scheduler's `WHERE retry_at <= now()` query.
    op.create_index("ix_orders_retry_at", "orders", ["retry_at"])

    # ── 5. notifications table ─────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "read_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_type", "notifications", ["type"])
    op.create_index("ix_notifications_read_at", "notifications", ["read_at"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    # notifications
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_read_at", table_name="notifications")
    op.drop_index("ix_notifications_type", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    # orders
    op.drop_index("ix_orders_retry_at", table_name="orders")
    op.drop_column("orders", "is_closing")
    op.drop_column("orders", "retry_attempted")
    op.drop_column("orders", "retry_at")

    # subscriber_settings
    op.drop_column("subscriber_settings", "retry_interval_close")
    op.drop_column("subscriber_settings", "retry_interval_open")

    # enum
    op.execute("DROP TYPE IF EXISTS retry_interval")

    # NOTE: Postgres can't drop a value from an enum. RETRY_PENDING stays in
    # order_status — harmless residual. To clean up, recreate the enum
    # without the value and re-cast the column, which is destructive.
