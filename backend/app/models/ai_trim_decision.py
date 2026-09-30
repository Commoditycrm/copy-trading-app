"""One AI trimming decision: what the model was asked, what it said, what we did.

Kept for every call — holds and errors included — because it is the only
record of WHY an automated exit happened, and because the cadence gate reads
the latest row to decide when to ask again.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AiTrimDecision(Base, TimestampMixin):
    __tablename__ = "ai_trim_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discord_position_guards.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    contract: Mapped[str] = mapped_column(String(80), nullable=False)

    model: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)       # suggest | auto

    # The market the model saw.
    mark: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    held: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # What it said, after validation. ``action`` is hold | trim | exit | raise_stop.
    action: Mapped[str] = mapped_column(String(12), nullable=False)
    sell_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0), nullable=False)
    new_stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Anything validation changed or refused, so a clipped decision is visible.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # hold | suggested | executed | paper | approved | dismissed | superseded
    # | expired | error
    status: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
