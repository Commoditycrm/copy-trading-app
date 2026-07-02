"""add disconnected_at to broker_accounts (reconnect history)

Soft-disconnect support: instead of hard-deleting a direct-credential
broker (Alpaca / IBKR) on Disconnect, we stamp ``disconnected_at`` and
keep the row — encrypted credentials and all — as a reconnectable history
entry. NULL = active (the historic behaviour); non-NULL = in history.

Reconnect re-validates the stored creds and clears this stamp. A partial
index keeps the "one ACTIVE broker per user" lookups cheap.

Revision ID: a7f3c1e9b204
Revises: f1e2d3c4b5a6
Create Date: 2026-07-01 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7f3c1e9b204"
down_revision: Union[str, None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broker_accounts",
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Active rows are the hot path (list + one-broker-per-user checks);
    # index just those so history rows don't bloat it.
    op.create_index(
        "ix_broker_accounts_active",
        "broker_accounts",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("disconnected_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_broker_accounts_active", table_name="broker_accounts")
    op.drop_column("broker_accounts", "disconnected_at")
