"""retag OCC option orders mis-tagged as STOCK

The Alpaca activity-sync path (fills_sync) classified instruments with a length
gate — len(symbol) >= 18 — but Alpaca's activity feed returns the UNPADDED OCC
symbol, so a 1-2 char ticker root is only 16-17 chars (T270115C00026000,
VG260626C00010500). Those fell through to STOCK, lost the 100x contract
multiplier, and showed realized P&L 100x too small (0.32 instead of 32.0). The
code now uses the shared OCC parser; this repairs the rows already written.

We reconstruct the option fields straight from the stored symbol (root + YYMMDD +
C/P + strike*1000) and rewrite the symbol to its root, so these rows key the same
as correctly-tagged legs of the same contract and pair up in the FIFO P&L walk.

Revision ID: b6f1d3a9c72e
Create Date: 2026-09-22
"""
from typing import Union

from alembic import op

revision: str = "b6f1d3a9c72e"
down_revision: Union[str, None] = "d5a1c7e42f86"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


# Trailing 15 chars are fixed (6 date + 1 right + 8 strike); the root is whatever
# precedes them, 1-6 chars. Slice by length rather than a fixed offset.
_RETAG = """
UPDATE orders
SET
    instrument_type = 'OPTION',
    option_expiry = make_date(
        2000 + substr(symbol, char_length(symbol) - 14, 2)::int,
        substr(symbol, char_length(symbol) - 12, 2)::int,
        substr(symbol, char_length(symbol) - 10, 2)::int
    ),
    option_strike = right(symbol, 8)::numeric / 1000,
    option_right = (CASE WHEN substr(symbol, char_length(symbol) - 8, 1) = 'C'
                         THEN 'CALL' ELSE 'PUT' END)::option_right,
    symbol = left(symbol, char_length(symbol) - 15)
WHERE instrument_type = 'STOCK'
  AND symbol ~ '^[A-Z.]{1,6}[0-9]{6}[CP][0-9]{8}$'
"""


def upgrade() -> None:
    op.execute(_RETAG)


def downgrade() -> None:
    # One-way data repair — the original mis-tagged rows were wrong. Rebuilding
    # the concatenated symbol and dropping the option fields would only re-corrupt
    # the P&L, so downgrade is intentionally a no-op.
    pass
