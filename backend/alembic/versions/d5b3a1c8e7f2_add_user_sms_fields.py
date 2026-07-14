"""add user SMS fields (phone, sms_notifications_enabled)

Revision ID: d5b3a1c8e7f2
Revises: c4a1e7f9b302
Create Date: 2026-07-13

Opt-in SMS: phone (E.164, nullable) + sms_notifications_enabled (default false
so existing rows never receive SMS until they explicitly opt in).
"""
from alembic import op
import sqlalchemy as sa

revision = "d5b3a1c8e7f2"
down_revision = "c4a1e7f9b302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(length=20), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "sms_notifications_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "sms_notifications_enabled")
    op.drop_column("users", "phone")
