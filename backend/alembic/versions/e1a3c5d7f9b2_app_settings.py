"""app_settings: global runtime key/value flags (admin-togglable)

Backs operational toggles an admin can flip at runtime (e.g. the live
market-data streams) without an env change + redeploy. The env var stays the
default; a row here overrides it when present.

Revision ID: e1a3c5d7f9b2
Revises: d9e2f4a6b8c1
"""
import sqlalchemy as sa
from alembic import op

revision = "e1a3c5d7f9b2"
down_revision = "d9e2f4a6b8c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
