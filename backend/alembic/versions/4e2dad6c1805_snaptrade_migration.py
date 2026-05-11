"""snaptrade migration

Revision ID: 4e2dad6c1805
Revises: 36f268704ea8
Create Date: 2026-05-09 15:20:24.050778

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4e2dad6c1805'
down_revision: Union[str, None] = '36f268704ea8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add SnapTrade identity columns to users.
    op.add_column(
        "users",
        sa.Column("snaptrade_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("encrypted_snaptrade_user_secret", sa.Text(), nullable=True),
    )

    # broker_accounts: drop the old shape (test data only) and recreate.
    # CASCADE removes the orders FK; orders themselves get cleared via the FK
    # cascade. There are no real orders at this point in the project.
    op.execute("DROP TABLE broker_accounts CASCADE")
    op.execute("DROP TYPE IF EXISTS broker_name")

    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("broker", sa.String(60), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "supports_fractional", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("snaptrade_account_id", sa.String(120), nullable=False),
        sa.Column("broker_account_number", sa.String(120), nullable=True),
        sa.Column(
            "connection_status",
            sa.String(40),
            nullable=False,
            server_default=sa.text("'connected'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("snaptrade_account_id"),
    )
    op.create_index("ix_broker_accounts_user_id", "broker_accounts", ["user_id"])
    op.create_index(
        "ix_broker_accounts_snaptrade_account_id",
        "broker_accounts",
        ["snaptrade_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_accounts_snaptrade_account_id", table_name="broker_accounts")
    op.drop_index("ix_broker_accounts_user_id", table_name="broker_accounts")
    op.drop_table("broker_accounts")
    op.drop_column("users", "encrypted_snaptrade_user_secret")
    op.drop_column("users", "snaptrade_registered")
