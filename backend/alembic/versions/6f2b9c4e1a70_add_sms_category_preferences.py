"""add per-category SMS preferences to users

Default TRUE so anyone who already opted into SMS keeps receiving these three
categories. They previously received every notification type by text, so this
is a net reduction in messages, never an increase.

Revision ID: 6f2b9c4e1a70
Revises: d5b3a1c8e7f2
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "6f2b9c4e1a70"
down_revision = "d5b3a1c8e7f2"
branch_labels = None
depends_on = None


_COLUMNS = (
    "sms_on_auto_actions",
    "sms_on_trade_rejected",
    "sms_on_broker_connection",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "users",
            sa.Column(
                name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)
