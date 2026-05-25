"""orders.broker_account_id: drop CASCADE, switch to SET NULL + nullable

Previously, disconnecting a broker (DELETE /api/brokers/{id}) wiped every
Order tied to that account because of two cascading delete rules:

  1. FK constraint:  ON DELETE CASCADE
  2. SQLAlchemy:     cascade="all, delete-orphan" on BrokerAccount.orders

That meant the Performance page + Order History silently lost the entire
fanout audit trail every time a user reconnected their broker. Since the
trade itself was already at the broker (Alpaca) and money had moved, this
was a serious data-loss footgun.

This migration:
  - Makes orders.broker_account_id NULLABLE
  - Replaces the FK with ON DELETE SET NULL
  - Preserves the index on the column

After this, deleting a broker_account leaves Order rows intact with
broker_account_id = NULL. They keep showing up in Performance and Order
History; cancel/close endpoints just 404 since there's no broker to talk
to any more (graceful — already handled by existing null checks in
trades.py / retry_scheduler.py / recovery.py via `if acct is None`).

The SQLAlchemy cascade is removed in the model file in the same commit;
no SQL change needed for that — it only affected ORM-level deletes which
flow through the FK constraint anyway.

Revision ID: d9f4a82e617c
Revises: c8d3f5a92e14
Create Date: 2026-05-25 14:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d9f4a82e617c"
down_revision: Union[str, None] = "c8d3f5a92e14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres auto-generated constraint name when no explicit name was set —
# matches `<table>_<col>_fkey`. Confirmed by inspecting the initial schema
# migration which used `sa.ForeignKeyConstraint([...], [...], ondelete=...)`
# without a name kwarg.
_FK_NAME = "orders_broker_account_id_fkey"


def upgrade() -> None:
    # 1. Allow NULL so SET NULL has somewhere to write.
    op.alter_column(
        "orders",
        "broker_account_id",
        existing_type=op.f("UUID"),
        nullable=True,
    )

    # 2. Replace the CASCADE FK with SET NULL.
    op.drop_constraint(_FK_NAME, "orders", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "orders",
        "broker_accounts",
        ["broker_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Roll back to CASCADE. This will only succeed cleanly if no rows
    # currently have broker_account_id IS NULL — those would violate the
    # NOT NULL we're about to re-impose. We refuse to silently delete them
    # here; an operator who really wants to roll back must decide whether
    # to backfill or delete those orphan rows first.
    op.execute(
        # Belt-and-braces: error early with a clear message rather than a
        # Postgres "null value in column" error during ALTER COLUMN.
        "DO $$ BEGIN "
        "  IF EXISTS (SELECT 1 FROM orders WHERE broker_account_id IS NULL) THEN "
        "    RAISE EXCEPTION "
        "      'cannot downgrade: orders.broker_account_id has NULL rows. "
        "       Backfill or delete them first.';"
        "  END IF; "
        "END $$;"
    )
    op.drop_constraint(_FK_NAME, "orders", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        "orders",
        "broker_accounts",
        ["broker_account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        "orders",
        "broker_account_id",
        existing_type=op.f("UUID"),
        nullable=False,
    )
