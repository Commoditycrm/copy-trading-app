"""add 'webull' value to broker_name enum

Adds Webull as a second supported broker alongside Alpaca. The connect
flow is meaningfully different from Alpaca (MFA-required login, polling-
based order updates via the unofficial `webull` SDK), but from the DB's
point of view it's just another value of `broker_name`. See
backend/app/brokers/webull.py for the adapter and
backend/app/services/webull_listener.py for the listener loop.

One-broker-per-user is enforced at the API layer
(backend/app/api/brokers.py), not the DB layer — leaving the schema
permissive keeps room for future multi-broker support without another
migration.

Revision ID: e8b2f7c3a91d
Revises: f1a2b3c4d5e6
Create Date: 2026-05-26 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e8b2f7c3a91d"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction in Postgres
    # before v12. All currently-supported versions are 12+, but we still
    # need the autocommit block because the surrounding alembic migration
    # opens a transaction by default.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'webull'")


def downgrade() -> None:
    # Postgres can't drop a value from an enum without recreating the type
    # and re-casting every column that uses it. See the c8d3f5a92e14
    # 'fake' migration's downgrade comment for the same reasoning — we
    # accept the residual value rather than do the destructive dance.
    pass
