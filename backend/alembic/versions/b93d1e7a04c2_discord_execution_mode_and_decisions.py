"""add Discord execution mode and per-alert decisions

Revision ID: b93d1e7a04c2
Revises: a7e3f92c1b04
Create Date: 2026-09-11 00:00:00.000000

Execution mode is per CHANNEL (auto/manual) and defaults to manual — it decides
whether an alert will one day reach a broker unattended, so the safe option has
to be the one you get by accident.

The decision is recorded on the MESSAGE, separately from its parse status: one
records what we understood, the other what the trader chose to do about it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM


revision: str = "b93d1e7a04c2"
down_revision: Union[str, None] = "a7e3f92c1b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DECISION = ENUM(
    "pending", "approved", "rejected",
    name="discord_signal_decision",
    create_type=False,
)


def upgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column("execution_mode", sa.String(10), nullable=False, server_default="manual"),
    )
    _DECISION.create(op.get_bind(), checkfirst=True)
    op.add_column("discord_messages", sa.Column("decision", _DECISION, nullable=True))
    op.add_column(
        "discord_messages", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("discord_messages", sa.Column("decision_mode", sa.String(10), nullable=True))
    op.create_index("ix_discord_messages_decision", "discord_messages", ["decision"])
    # Alerts already parsed before this existed are awaiting a decision.
    op.execute("UPDATE discord_messages SET decision = 'pending' WHERE status = 'parsed'")


def downgrade() -> None:
    op.drop_index("ix_discord_messages_decision", table_name="discord_messages")
    op.drop_column("discord_messages", "decision_mode")
    op.drop_column("discord_messages", "decided_at")
    op.drop_column("discord_messages", "decision")
    _DECISION.drop(op.get_bind(), checkfirst=True)
    op.drop_column("discord_alert_sources", "execution_mode")
