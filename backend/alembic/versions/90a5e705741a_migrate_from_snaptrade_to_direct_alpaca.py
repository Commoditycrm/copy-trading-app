"""migrate from snaptrade to direct alpaca

Revision ID: 90a5e705741a
Revises: a838e919b693
Create Date: 2026-05-13 19:05:03.700058

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '90a5e705741a'
down_revision: Union[str, None] = 'a838e919b693'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop SnapTrade-specific columns from users.
    op.drop_column("users", "encrypted_snaptrade_user_secret")
    op.drop_column("users", "snaptrade_registered")

    # 2. Replace broker_accounts shape.
    # Existing SnapTrade connections won't translate (different auth model) — we
    # wipe broker_accounts (and dependent orders/fills via CASCADE) so users
    # reconnect cleanly with their Alpaca API keys.
    op.execute("DROP TABLE broker_accounts CASCADE")
    # Drop any orphaned enum type from earlier failed runs.
    op.execute("DROP TYPE IF EXISTS broker_name")

    # 3. Re-create broker_accounts with the new shape. create_table auto-creates
    #    the broker_name enum from the Column definition.
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("broker", sa.Enum("alpaca", name="broker_name"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supports_fractional", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("broker_account_number", sa.String(120), nullable=True),
        sa.Column(
            "connection_status", sa.String(40), nullable=False,
            server_default=sa.text("'connected'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("cash", sa.Numeric(20, 4), nullable=True),
        sa.Column("buying_power", sa.Numeric(20, 4), nullable=True),
        sa.Column("total_equity", sa.Numeric(20, 4), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("balance_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_broker_accounts_user_id", "broker_accounts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_broker_accounts_user_id", table_name="broker_accounts")
    op.execute("DROP TABLE broker_accounts CASCADE")
    op.execute("DROP TYPE IF EXISTS broker_name")
    op.add_column("users", sa.Column("snaptrade_registered", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("encrypted_snaptrade_user_secret", sa.Text(), nullable=True))
