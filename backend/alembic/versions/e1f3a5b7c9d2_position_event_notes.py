"""Position events: why each one happened

position_events.note — the reason ("by you on Positions", "Mark's alert: …"),
position_events.order_id — the order an order_note explains.

Revision ID: e1f3a5b7c9d2
Revises: d0e2f4a6b8c1
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1f3a5b7c9d2"
down_revision = "d0e2f4a6b8c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("position_events", sa.Column("note", sa.String(300), nullable=True))
    op.add_column("position_events", sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_position_events_order_id", "position_events", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_position_events_order_id", table_name="position_events")
    op.drop_column("position_events", "order_id")
    op.drop_column("position_events", "note")
