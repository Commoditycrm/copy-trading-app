"""add 'fake' value to broker_name enum

Lets us route subscribers to the test-only FakeBrokerAdapter for load-
testing the fanout pipeline without burning Alpaca paper accounts. See
backend/app/brokers/fake.py and scripts/seed_fake_subscribers.py.

The new enum value is harmless on its own — no existing rows reference it,
and adapter_for() raises if a real subscriber somehow gets routed to it.

Revision ID: c8d3f5a92e14
Revises: b3c1d4e2a51f
Create Date: 2026-05-23 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c8d3f5a92e14"
down_revision: Union[str, None] = "b3c1d4e2a51f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction in Postgres
    # before v12. All currently-supported versions are 12+, but we still
    # need the autocommit block because the surrounding alembic migration
    # opens a transaction by default.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'fake'")


def downgrade() -> None:
    # Postgres can't drop a value from an enum without recreating the type
    # and re-casting every column that uses it. Since 'fake' is only
    # referenced by test rows (and we want it harmless to leave), we
    # accept the residual value rather than do the destructive dance.
    # If you really need to remove it, follow the documented pattern at
    # https://www.postgresql.org/docs/current/datatype-enum.html
    pass
