"""SMS notifications: one row per text decided on, with Twilio delivery state

Revision ID: a9c4e2f71b38
Revises: f3a7c1e85b02

For rejected-trade texts (services/rejected_trade_sms.py). The unique
dedup_key is what keeps a rejection seen by several paths to one text. A new,
empty table: nothing reads or writes it until REJECTED_TRADE_SMS_ENABLED is on.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a9c4e2f71b38"
down_revision = "f3a7c1e85b02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False, unique=True),
        sa.Column("subject_key", sa.String(160), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("phone_last4", sa.String(4), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skip_reason", sa.String(48), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("twilio_sid", sa.String(64), nullable=True),
        sa.Column("twilio_status", sa.String(24), nullable=True),
        sa.Column("twilio_error_code", sa.Integer(), nullable=True),
        sa.Column("num_segments", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(12, 5), nullable=True),
        sa.Column("price_unit", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for col in ("user_id", "order_id", "status", "next_attempt_at", "twilio_sid", "created_at", "subject_key"):
        op.create_index(f"ix_sms_notifications_{col}", "sms_notifications", [col])


def downgrade() -> None:
    op.drop_table("sms_notifications")
