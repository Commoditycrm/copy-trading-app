"""add business_name to users

Trader-only business / brand name that's surfaced as the app name in the
shell — both for the trader themselves and for every subscriber who
follows them. Required at registration for role=trader (enforced in the
API layer); column is nullable here so existing rows and subscriber rows
remain valid without a backfill.

Revision ID: c7a9d0e2f481
Revises: b5e9d8f3a4c2
Create Date: 2026-06-10 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a9d0e2f481"
down_revision: Union[str, None] = "b5e9d8f3a4c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("business_name", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "business_name")
