"""add discord_messages (inbound alert-copying, step 3)

Revision ID: c8f1b3d29e04
Revises: b6e4a2c81d37
Create Date: 2026-09-09 00:00:00.000000

Durable record of every message observed in a watched channel, written BEFORE
anything tries to parse or act on it.

The unique constraint here is the real duplicate guard for the whole feature:
Discord's message id scoped per source. Until now dedup lived in a Redis marker
that fails open, which was acceptable only because nothing downstream could
place an order yet. From here the database enforces it, so a reconnect, restart,
retry or Redis outage cannot produce a second order.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID


revision: str = "c8f1b3d29e04"
down_revision: Union[str, None] = "b6e4a2c81d37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False: the type is created explicitly in upgrade() with
# checkfirst, so the column must NOT try to emit a second CREATE TYPE during
# create_table (which fails with DuplicateObject, and leaves a half-applied
# migration behind — the type present, the table missing).
_STATUS = ENUM(
    "received", "ignored", "parsed", "invalid", "order_created", "order_failed",
    name="discord_message_status",
    create_type=False,
)


def upgrade() -> None:
    # checkfirst so a re-run after a partial failure is a no-op rather than an error.
    _STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "discord_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True),
                  sa.ForeignKey("discord_alert_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("discord_message_id", sa.String(40), nullable=False),
        sa.Column("discord_channel_id", sa.String(40), nullable=False),
        sa.Column("discord_server_id", sa.String(40), nullable=True),
        sa.Column("author", sa.String(200), nullable=True),
        sa.Column("author_id", sa.String(40), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attachments", JSONB, nullable=False, server_default="[]"),
        sa.Column("embeds", JSONB, nullable=False, server_default="[]"),
        sa.Column("status", _STATUS, nullable=False, server_default="received"),
        sa.Column("status_reason", sa.String(500), nullable=True),
        sa.Column("order_id", UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_discord_messages_source_id", "discord_messages", ["source_id"])
    op.create_index("ix_discord_messages_user_id", "discord_messages", ["user_id"])
    op.create_index("ix_discord_messages_discord_message_id", "discord_messages",
                    ["discord_message_id"])
    op.create_index("ix_discord_messages_status", "discord_messages", ["status"])
    op.create_index("ix_discord_messages_order_id", "discord_messages", ["order_id"])
    op.create_index("ix_discord_messages_source_created", "discord_messages",
                    ["source_id", "created_at"])
    # THE duplicate guard for the whole inbound feature.
    op.create_unique_constraint(
        "uq_discord_message_source_msg", "discord_messages",
        ["source_id", "discord_message_id"],
    )


def downgrade() -> None:
    op.drop_table("discord_messages")
    _STATUS.drop(op.get_bind(), checkfirst=True)
