"""trader_settings.discord_manual_exit — exits left to the trader

Off (the default, and what every existing trader keeps), a position opened from
a Discord alert is exited by the channel's exit alerts or by auto-trim. On,
Kopyya never sells it: exit alerts are recorded but not acted on, and neither
auto-trim nor AI trimming touches it.

Revision ID: d4e6f8a0c2b1
Revises: a3c5e7f9b1d2
"""
import sqlalchemy as sa
from alembic import op

revision = "d4e6f8a0c2b1"
down_revision = "a3c5e7f9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column(
            "discord_manual_exit", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_manual_exit")
