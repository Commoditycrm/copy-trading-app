"""add parsed_signals (multiple trades per Discord message)

Revision ID: f1c8a05de234
Revises: e2b6d431f70a
Create Date: 2026-09-10 00:00:00.000000

Alert channels post blocks of exits in a single message; keeping only the first
parsed trade silently dropped the rest.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "f1c8a05de234"
down_revision: Union[str, None] = "e2b6d431f70a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "discord_messages",
        sa.Column("parsed_signals", JSONB, nullable=False, server_default="[]"),
    )
    # Backfill so existing rows aren't a special case for readers.
    op.execute(
        "UPDATE discord_messages SET parsed_signals = jsonb_build_array(parsed_signal) "
        "WHERE parsed_signal IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("discord_messages", "parsed_signals")
