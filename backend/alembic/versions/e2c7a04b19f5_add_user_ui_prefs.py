"""user_ui_prefs — per-user UI prefs (configurable table columns)

One row per user holding a JSONB bucket of UI preferences. First use is
per-table column config (visibility, order, widths); the column is general so
future UI prefs need no new table.

Revision ID: e2c7a04b19f5
Revises: c3a81f5e27d4
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2c7a04b19f5"
down_revision = "c3a81f5e27d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_ui_prefs",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "column_prefs", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_ui_prefs")
