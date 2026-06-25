"""add email_verified to users

Adds email verification state for the signup verification flow. Soft
enforcement: an unverified user can still log in, but the app shows a
"verify your email" banner until they confirm.

Existing users are grandfathered to verified=true in the upgrade so the
banner only ever nags genuinely-new signups, not the current user base.

Revision ID: c5e91a7b3d24
Revises: e9f1c7d2a803
Create Date: 2026-06-15 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e91a7b3d24"
down_revision: Union[str, None] = "e9f1c7d2a803"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Grandfather all existing users so the verification banner only ever
    # targets new signups — never the current user base.
    op.execute("UPDATE users SET email_verified = true")


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
