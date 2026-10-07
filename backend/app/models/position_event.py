import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PositionEvent(Base):
    """Something that happened to a position's protection: a stop set, moved
    or removed, a trailing stop armed or raised, a trailing exit armed.

    Orders and fills are already rows of their own; this records what only
    ever lived as the CURRENT value on the Discord guard, so the Position
    summary can show its history. Written by services/position_events from
    guard changes at flush time. Keyed by contract, like the guard.
    """

    __tablename__ = "position_events"
    __table_args__ = (
        Index("ix_position_events_contract", "user_id", "symbol", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    option_strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(4), nullable=True)
    option_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    # stop_set | stop_moved | stop_removed | trailing_stop_set | trailing_stop_raised
    # | trailing_exit_armed | trailing_exit_cleared
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)       # the level now
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)   # the level before
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    trail_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)
    trail_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    peak: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
