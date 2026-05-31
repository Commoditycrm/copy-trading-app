"""add_brokerage_name_to_broker_accounts

Adds a denormalized ``brokerage_name`` column so the trader-facing fanout
table can show *which* broker each SnapTrade-routed subscriber actually
connected (Webull / Robinhood / IBKR / …) instead of the generic
"snaptrade". The value was already captured in the Fernet-encrypted
credentials blob at connect time — this migration backfills existing
rows by decrypting once on the migration's own connection (using
op.get_bind so we don't deadlock against alembic's transaction the way
a fresh SessionLocal would).

Revision ID: 33ee68c24d53
Revises: c94e2206daac
Create Date: 2026-05-31 10:25:42.008893
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '33ee68c24d53'
down_revision: Union[str, None] = 'c94e2206daac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'broker_accounts',
        sa.Column('brokerage_name', sa.String(length=120), nullable=True),
    )

    # Backfill: every existing row where broker='snaptrade' has its
    # underlying brokerage name (Webull / Robinhood / …) stashed in the
    # encrypted credentials JSON. Decrypt each row on the migration's
    # OWN connection (op.get_bind) so we don't open a parallel pool
    # connection and deadlock against alembic's open transaction.
    from app.services.crypto import decrypt_json

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, encrypted_credentials FROM broker_accounts "
            "WHERE broker = 'snaptrade'"
        )
    ).fetchall()

    for row_id, blob in rows:
        try:
            creds = decrypt_json(blob)
        except Exception:  # noqa: BLE001
            # Stale / wrong-key creds — leave brokerage_name NULL, the
            # serializer will fall back to broker.value ("snaptrade")
            # for these, matching pre-migration behaviour.
            continue
        name = (creds.get("brokerage_name") or "").strip()
        if name:
            bind.execute(
                sa.text(
                    "UPDATE broker_accounts SET brokerage_name = :n "
                    "WHERE id = :i"
                ),
                {"n": name, "i": row_id},
            )


def downgrade() -> None:
    op.drop_column('broker_accounts', 'brokerage_name')
