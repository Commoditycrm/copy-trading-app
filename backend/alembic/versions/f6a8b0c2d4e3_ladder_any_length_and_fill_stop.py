"""A ladder of any length, and a stop on fill

trader_settings:
  discord_trim_count     how many trims the ladder has (3 for everyone today)
  discord_extra_trims    trims past the third, as JSON
  discord_fill_stop_pct  "On Fill" stop, as a return from entry; NULL = none
discord_position_guards:
  fill_stop_done         the On Fill stop was handled for this holding

Existing rows are marked done, so a position already open when this deploys
does not suddenly get a stop it never had.

Revision ID: f6a8b0c2d4e3
Revises: e5f7a9b1c3d2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6a8b0c2d4e3"
down_revision = "e5f7a9b1c3d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_trim_count", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("trader_settings", sa.Column(
        "discord_extra_trims", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("trader_settings", sa.Column(
        "discord_fill_stop_pct", sa.Numeric(9, 4), nullable=True))
    op.add_column("discord_position_guards", sa.Column(
        "fill_stop_done", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.execute("UPDATE discord_position_guards SET fill_stop_done = true")


def downgrade() -> None:
    op.drop_column("discord_position_guards", "fill_stop_done")
    op.drop_column("trader_settings", "discord_fill_stop_pct")
    op.drop_column("trader_settings", "discord_extra_trims")
    op.drop_column("trader_settings", "discord_trim_count")
