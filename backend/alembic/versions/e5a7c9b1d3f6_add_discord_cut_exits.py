"""Manual exits: auto exit on the trader's cut

trader_settings.discord_cut_exits — with Manual exits, still sell when the
trader says they are cutting (full or partial). Default true (ticked). Per
channel like the other exit settings.

Revision ID: e5a7c9b1d3f6
Revises: d4f6a8c0e2b3
"""
import sqlalchemy as sa
from alembic import op

revision = "e5a7c9b1d3f6"
down_revision = "d4f6a8c0e2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_cut_exits", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_cut_exits")
