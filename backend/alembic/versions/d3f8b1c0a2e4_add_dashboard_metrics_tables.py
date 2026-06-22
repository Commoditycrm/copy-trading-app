"""add dashboard metrics tables (test_results, load_test_runs)

Append-only logs feeding the admin Performance & Testing dashboard:
  - test_results   : one row per test-suite run (pass/fail/skip counts).
  - load_test_runs : one row per load-test run (subs, total time, waves, errors).

No FKs / business data, so no cascade concerns.

Revision ID: d3f8b1c0a2e4
Revises: 9434aece43f6
Create Date: 2026-06-22 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d3f8b1c0a2e4"
down_revision: Union[str, None] = "9434aece43f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suite", sa.String(length=120), nullable=False),
        sa.Column("passed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_results_suite", "test_results", ["suite"])
    op.create_index("ix_test_results_created_at", "test_results", ["created_at"])

    op.create_table(
        "load_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscribers", sa.Integer(), nullable=False),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("waves", sa.Integer(), nullable=True),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_load_test_runs_created_at", "load_test_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_load_test_runs_created_at", table_name="load_test_runs")
    op.drop_table("load_test_runs")
    op.drop_index("ix_test_results_created_at", table_name="test_results")
    op.drop_index("ix_test_results_suite", table_name="test_results")
    op.drop_table("test_results")
