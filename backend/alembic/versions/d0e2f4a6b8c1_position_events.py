"""Position events

position_events — the history of a position's stop: set, moved, removed,
trailing armed and raised. Shown in the Position summary alongside orders.

Revision ID: d0e2f4a6b8c1
Revises: c9d1e3f5a7b9
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d0e2f4a6b8c1"
down_revision = "c9d1e3f5a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("option_strike", sa.Numeric(18, 4), nullable=True),
        sa.Column("option_right", sa.String(4), nullable=True),
        sa.Column("option_expiry", sa.Date(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=True),
        sa.Column("old_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=True),
        sa.Column("trail_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("trail_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("peak", sa.Numeric(18, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_position_events_contract", "position_events", ["user_id", "symbol", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_position_events_contract", table_name="position_events")
    op.drop_table("position_events")
