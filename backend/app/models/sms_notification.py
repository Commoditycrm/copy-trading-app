"""One text message Kopyya decided to send, and what became of it.

Written by services/rejected_trade_sms.py. ``dedup_key`` is unique, so the
same rejection reported by the placement path, the order stream, a poll and
the fill sync still produces one row — and one text. The worker sends pending
rows, retries the transient failures, and reads Twilio's delivery status back
onto the row for Admin → Rejected.

The phone number itself is not stored: it's read from the user at send time
(so a changed or removed number is respected); only the last four digits are
kept for the admin view.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SmsNotification(Base):
    __tablename__ = "sms_notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)

    category: Mapped[str] = mapped_column(String(32), nullable=False)          # "trade_rejected"
    # broker_rejected | blocked | failed | needs_review — decides the heading
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # user + contract + side: a retry loop writes a new rejected order each
    # pass, and those must not each text
    subject_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    phone_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # pending → sent → delivered | undelivered | failed; or skipped (opt-out,
    # no phone) / failed (Twilio refused, or retries ran out)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    twilio_sid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    twilio_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    twilio_error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_segments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 5), nullable=True)
    price_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
