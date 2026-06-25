"""Append-only metric logs for the admin Performance & Testing dashboard.

Two small tables, both write-once:

  - ``test_results``   — one row per test-suite run (pass/fail counts). Written
    by the test runner / CI (or demo_smoke_test.py); the dashboard reads the
    latest row per suite.
  - ``load_test_runs`` — one row per load-test run (subscriber count, total
    time, broker waves, errors). The dashboard shows recent history.

Neither carries business data, so there are no FKs and no cascade concerns.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    suite: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Where the run came from: "ci" / "manual" / "smoke", etc.
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )


class LoadTestRun(Base):
    __tablename__ = "load_test_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscribers: Mapped[int] = mapped_column(Integer, nullable=False)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True,
    )
