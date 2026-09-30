"""AI trimming: exit-engine choice, OpenRouter settings, decision log

Adds the trader-level switch between the trim ladder and an AI exit engine,
its settings, and ai_trim_decisions — one row per model call, the only record
of why an automated exit happened.

Revision ID: b7e1c4a9d2f0
Revises: 9c4d7e2f6a13
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b7e1c4a9d2f0"
down_revision = "9c4d7e2f6a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_exit_engine", sa.String(10), server_default="ladder", nullable=False))
    op.add_column("trader_settings", sa.Column(
        "discord_ai_mode", sa.String(10), server_default="suggest", nullable=False))
    op.add_column("trader_settings", sa.Column(
        "discord_ai_model", sa.String(120),
        server_default="anthropic/claude-sonnet-5.5", nullable=False))
    op.add_column("trader_settings", sa.Column(
        "discord_ai_move_pct", sa.Numeric(9, 4), server_default="5", nullable=False))
    op.add_column("trader_settings", sa.Column(
        "discord_ai_min_interval_s", sa.Integer(), server_default="60", nullable=False))
    op.add_column("trader_settings", sa.Column(
        "discord_ai_instructions", sa.Text(), nullable=True))

    op.create_table(
        "ai_trim_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("guard_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("discord_position_guards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("contract", sa.String(80), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("mark", sa.Numeric(18, 4), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("held", sa.Numeric(18, 6), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("sell_qty", sa.Numeric(18, 6), server_default="0", nullable=False),
        sa.Column("new_stop_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_trim_decisions_user_id", "ai_trim_decisions", ["user_id"])
    op.create_index("ix_ai_trim_decisions_guard_id", "ai_trim_decisions", ["guard_id"])
    op.create_index("ix_ai_trim_decisions_status", "ai_trim_decisions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ai_trim_decisions_status", table_name="ai_trim_decisions")
    op.drop_index("ix_ai_trim_decisions_guard_id", table_name="ai_trim_decisions")
    op.drop_index("ix_ai_trim_decisions_user_id", table_name="ai_trim_decisions")
    op.drop_table("ai_trim_decisions")
    for col in ("discord_ai_instructions", "discord_ai_min_interval_s", "discord_ai_move_pct",
                "discord_ai_model", "discord_ai_mode", "discord_exit_engine"):
        op.drop_column("trader_settings", col)
