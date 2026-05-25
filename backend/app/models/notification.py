"""In-app notifications. Persistent so the subscriber can see them on next
login even if their browser was closed when the event happened.

Today the only notification type is ``copy.retry_failed`` (subscriber's
mirror retry exhausted on broker-disconnect failure), but the table is
generic so future types reuse it without a schema change.

Auto-deletion (30-day retention) is done inline on read — see
``services/notifications.py::create_notification`` for the cleanup
trigger. Avoids needing a cron job on Render's free tier.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Short machine-readable category — frontend can switch on it for icons
    # / colors / routing. Indexed so we can filter inbox by type later.
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # User-facing message. Plain text; UI applies styling around it.
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Free-form JSON for context: order_id, symbol, reason, trader name, etc.
    # Used by the frontend to build "View order" links and provide tooltips.
    metadata_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None,
    )

    # NULL = unread. Set to now() when the subscriber dismisses / opens.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    user = relationship("User", foreign_keys=[user_id])
