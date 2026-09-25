"""trader_settings.discord_auto_trim — run the exit ladder off the price

Off (the default, and the behaviour every existing trader keeps), a rung fires
when its Discord alert arrives. On, the poller fires it the moment the rung's
profit gate is reached, with no alert involved and through the same execution
path.

Revision ID: c3a81f5e27d4
Revises: b7f21c4d8e93
"""
import sqlalchemy as sa
from alembic import op

revision = "c3a81f5e27d4"
down_revision = "b7f21c4d8e93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column(
            "discord_auto_trim", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_auto_trim")
