"""Last trading day of the week: what a risky alert does

trader_settings.discord_last_day_risky — "normal" | "half" | "full". Account
level. On the week's last trading day (usually Friday; Thursday when Friday is
a market holiday) an entry the author called risky / light / a lotto is:
"normal" — half size, as on any day; "half" — half of that (a quarter); "full" —
the risky words are ignored and the entry is full size.

Revision ID: d4f6a8c0e2b3
Revises: c9f2a71b4e38
"""
import sqlalchemy as sa
from alembic import op

revision = "d4f6a8c0e2b3"
down_revision = "c9f2a71b4e38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_last_day_risky", sa.String(8), nullable=False, server_default="normal"))


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_last_day_risky")
