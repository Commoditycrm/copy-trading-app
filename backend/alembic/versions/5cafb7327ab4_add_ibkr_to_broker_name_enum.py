"""add ibkr to broker_name enum

Revision ID: 5cafb7327ab4
Revises: 6913e53668fa
Create Date: 2026-05-30 11:01:18.026667

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5cafb7327ab4'
down_revision: Union[str, None] = '6913e53668fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'ibkr' as a new value to the broker_name Postgres enum.
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction in older
    # Postgres releases; using COMMIT to be safe across versions.
    op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'ibkr'")


def downgrade() -> None:
    # Postgres has no native "DROP VALUE" for enum types. Downgrading this
    # cleanly requires recreating the enum without 'ibkr' and migrating any
    # rows that use it. Left as a no-op — restore from a pre-upgrade dump
    # if a rollback is truly needed.
    pass
