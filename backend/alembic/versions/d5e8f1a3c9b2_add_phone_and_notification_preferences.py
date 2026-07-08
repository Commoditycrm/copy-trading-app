"""add phone fields + notification_preferences

Revision ID: d5e8f1a3c9b2
Revises: c4a1e7f9b302
Create Date: 2026-07-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd5e8f1a3c9b2'
down_revision: Union[str, None] = 'c4a1e7f9b302'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phone + SMS verification on users.
    op.add_column('users', sa.Column('phone_number', sa.String(length=20), nullable=True))
    op.add_column(
        'users',
        sa.Column('phone_verified', sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column('users', sa.Column('phone_verified_at', sa.DateTime(timezone=True), nullable=True))

    # Per-user notification channel preferences.
    op.create_table(
        'notification_preferences',
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email_enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('sms_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('event_overrides', postgresql.JSONB(), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    op.drop_table('notification_preferences')
    op.drop_column('users', 'phone_verified_at')
    op.drop_column('users', 'phone_verified')
    op.drop_column('users', 'phone_number')
