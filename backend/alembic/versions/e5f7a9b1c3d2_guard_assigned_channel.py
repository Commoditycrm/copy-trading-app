"""discord_position_guards.source_id — a holding's channel, assigned by hand

NULL (every existing row) keeps today's behaviour: a position belongs to the
channel whose alert opened it. Set from the Positions page, it overrides that —
for the Channel column, the exit settings that apply, and which channel's
alerts reach the position.

Revision ID: e5f7a9b1c3d2
Revises: d4e6f8a0c2b1
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5f7a9b1c3d2"
down_revision = "d4e6f8a0c2b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discord_position_guards",
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("discord_alert_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("discord_position_guards", "source_id")
