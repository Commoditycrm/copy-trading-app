"""WhatsApp opt-in: users.whatsapp_notifications_enabled + whatsapp_opt_in_at

Revision ID: b4d8e2a6c1f9
Revises: a9c4e2f71b38

Additive only: a NOT NULL boolean with a server default of false and a
nullable timestamp, so existing rows need no backfill and code without these
columns keeps working against the upgraded table.
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d8e2a6c1f9"
down_revision = "a9c4e2f71b38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("whatsapp_notifications_enabled", sa.Boolean(),
                                     server_default=sa.false(), nullable=False))
    op.add_column("users", sa.Column("whatsapp_opt_in_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "whatsapp_opt_in_at")
    op.drop_column("users", "whatsapp_notifications_enabled")
