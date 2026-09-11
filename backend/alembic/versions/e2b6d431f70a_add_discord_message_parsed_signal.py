"""add parsed_signal to discord_messages

Revision ID: e2b6d431f70a
Revises: d4a7c9e12b58
Create Date: 2026-09-10 00:00:00.000000

Stores what the parser read out of each alert. Denormalised on purpose: the
parser will change over time, and the audit trail must show what we actually
understood when the message arrived, not what today's code would make of it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "e2b6d431f70a"
down_revision: Union[str, None] = "d4a7c9e12b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("discord_messages", sa.Column("parsed_signal", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("discord_messages", "parsed_signal")
