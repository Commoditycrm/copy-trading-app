"""Per-user, per-day realized-P&L snapshot — the durable source for the Calendar.

Why this exists
---------------
The Calendar's daily realized P&L was computed on the fly from our stored
orders/fills. That has two problems:

  1. It DRIFTS — the SnapTrade listener misses closes (recorded canceled), so
     the DB is incomplete and days read wrong or blank.
  2. It can't be recomputed from the broker LIVE across broker changes — a live
     pull only sees the currently-connected broker, so days traded on a broker
     the user has since disconnected would vanish.

This table fixes both. A daily job computes each day's realized P&L from the
broker's OWN complete activity feed (broker_pnl.realized_by_day_from_broker) —
correct, no DB drift — and freezes it here, keyed by (user_id, day). The
Calendar then reads these snapshots.

Because the key is (user_id, day) and broker_account_id is only a nullable
reference (ON DELETE SET NULL), a snapshot SURVIVES the user disconnecting or
switching brokers: day 1's value (taken while Webull was connected) and day 8's
(taken while Alpaca was connected) both persist, so the Calendar stays correct
and continuous no matter how often the broker changes.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DailyRealizedPnlSnapshot(Base):
    """Frozen realized P&L for one user on one market day.

    Idempotent upsert via the (user_id, day) unique key — the daily job can
    re-run safely and simply overwrites the day's figure with the latest
    broker-computed value.
    """

    __tablename__ = "daily_realized_pnl_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_daily_realized_pnl_user_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Market day (ET) the figure is for. Plain DATE so the unique key and
    # per-day lookups are simple equality.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Which broker/account sourced this day (reference only — SET NULL so the
    # snapshot outlives a broker disconnect, which is the whole point).
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    broker: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # "broker_activities" (computed from the broker feed) or "db_fallback".
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="broker_activities")

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False,
    )
