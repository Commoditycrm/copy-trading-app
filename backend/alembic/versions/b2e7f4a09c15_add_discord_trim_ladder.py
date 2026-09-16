"""add the Discord trim ladder: guard state + configurable thresholds

A Discord exit alert now works a position down in three steps instead of
flattening it, so the guard has to remember what the position cost and what is
currently protecting it:

  entry_price   the FIRST fill, held fixed — every level keys off this
  stop_price    hard stop on what's still held (below entry, then break-even)
  trail_qty     a quantity leaving on a trailing stop rather than at market
  trail_amount  the dollar give-back that triggers that exit

The four thresholds live on trader_settings so they can be tuned without a code
change. Defaults match the agreed ladder: gate at +20%, stop 25% below entry,
trail instead of market above $0.90 entry, $0.25 of give-back.

All columns are additive and nullable or defaulted, so existing positions and
every non-Discord order path are unaffected.

Revision ID: b2e7f4a09c15
Revises: a1d5c8e30f76
Create Date: 2026-09-16
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2e7f4a09c15"
down_revision: Union[str, None] = "a1d5c8e30f76"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_GUARD_COLS = (
    ("entry_price", sa.Numeric(18, 4)),
    ("stop_price", sa.Numeric(18, 4)),
    ("trail_qty", sa.Numeric(18, 6)),
    ("trail_amount", sa.Numeric(18, 4)),
)

_SETTINGS_COLS = (
    ("discord_trim_profit_gate_pct", sa.Numeric(9, 4), "20"),
    ("discord_trim_stop_pct", sa.Numeric(9, 4), "25"),
    ("discord_trim_price_threshold", sa.Numeric(18, 4), "0.90"),
    ("discord_trim_trail_amount", sa.Numeric(18, 4), "0.25"),
)


def upgrade() -> None:
    for name, type_ in _GUARD_COLS:
        op.add_column(
            "discord_position_guards", sa.Column(name, type_, nullable=True)
        )
    for name, type_, default in _SETTINGS_COLS:
        op.add_column(
            "trader_settings",
            sa.Column(name, type_, nullable=False, server_default=default),
        )


def downgrade() -> None:
    for name, _type, _default in _SETTINGS_COLS:
        op.drop_column("trader_settings", name)
    for name, _type in _GUARD_COLS:
        op.drop_column("discord_position_guards", name)
