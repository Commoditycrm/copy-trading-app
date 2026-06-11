"""Per-account, per-UTC-day equity snapshot.

Why this exists
---------------
Some brokers don't expose a reliable "yesterday's market close" or
"day-start equity" through their API:

  * Direct Alpaca: ``GET /v2/account`` returns ``last_equity`` → ✅
  * SnapTrade → Alpaca paper: ``get_user_account_details`` payload
    does not populate ``day_start_total`` / ``beginning_of_day`` /
    ``equity_previous_close`` / ``previous_close`` for paper accounts
    → ❌ ``beginning_day_balance`` comes back ``None``

Without a day-start figure, ``todays_pl = equity - day_start`` can't
be computed, the Settings panel shows $0 today, and every percent-based
kill switch (``daily_loss_limit_pct``, ``daily_profit_limit_pct``,
``max_account_pct_per_day``) silently degrades — they all multiply
``beginning_day_balance * pct/100`` to derive their dollar threshold,
and that's ``None * 0.05 = silently skipped``.

This table fixes the gap broker-agnostically: ``pnl_poller``'s first
observation of each UTC day per account writes the current equity as
the day-start. Every subsequent poll that same day reads it back and
uses it as the baseline. Tomorrow at 00:00 UTC a fresh row is written
and the cycle repeats.

Direct Alpaca is unaffected — the poller continues to prefer the
broker-provided ``last_equity`` when available and only falls back to
this table when ``beginning_day_balance`` is ``None``.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DailyEquitySnapshot(Base):
    """First-observed equity for ``broker_account_id`` on ``utc_date``.

    Idempotent insert via the ``(broker_account_id, utc_date)`` unique
    constraint — pnl_poller races between multiple Python processes
    would otherwise produce duplicate rows. The unique key turns the
    second-writer's INSERT into an IntegrityError which the helper
    catches and re-reads."""

    __tablename__ = "daily_equity_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "utc_date", name="uq_daily_equity_account_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # UTC date the snapshot is for. Stored as DATE (not datetime) so
    # the unique key + per-day lookup work as plain equality.
    utc_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Equity in account currency at first poll of this UTC day.
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    # When the row was inserted (wall-clock, not the date the snapshot
    # is for). Useful for diagnosing "snapshot taken at 14:00 UTC, not
    # 00:00" cases that arise from a mid-day backend restart.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
