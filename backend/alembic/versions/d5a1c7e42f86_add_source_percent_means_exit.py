"""add discord_alert_sources.percent_means_exit

House style differs per channel, and the same text means opposite things.

Some channels mark an exit with scissors ("✂️ $SPY 760c +58%") and post bare
percentages as running P&L on a position they still hold — the same contract
reappearing at +24%, +45%, +60%. Others never use scissors and the percentage IS
the trim ("IWM 287P +52%", "AMD 27%").

Reading a P&L update as an exit would fire three sells for one position, so this
is opt-in per source. Default false keeps every existing channel behaving
exactly as it does today.

Revision ID: d5a1c7e42f86
Create Date: 2026-09-21
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5a1c7e42f86"
down_revision: Union[str, None] = "f3b8c91a2e47"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "discord_alert_sources",
        sa.Column(
            "percent_means_exit", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("discord_alert_sources", "percent_means_exit")
