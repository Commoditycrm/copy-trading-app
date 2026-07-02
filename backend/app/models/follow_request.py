"""Follow-request / approval between a subscriber and a trader.

Before this existed, a subscriber set ``SubscriberSettings.following_trader_id``
directly and mirroring started immediately. Now a subscriber must REQUEST to
follow, and the trader approves or rejects. Approval grants *permission* only —
the subscriber then chooses to follow (or later unfollow / re-follow) an
approved trader without asking again.

One row per (subscriber, trader) pair — a rejected request is re-opened back
to ``pending`` on a new ask rather than piling up duplicate rows.
"""
import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from datetime import datetime

from app.models.base import Base, TimestampMixin


class FollowRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FollowRequest(Base, TimestampMixin):
    __tablename__ = "follow_requests"
    # At most one relationship row per (subscriber, trader). Re-requesting
    # flips the existing row back to pending instead of inserting a duplicate.
    __table_args__ = (
        UniqueConstraint("subscriber_id", "trader_id", name="uq_follow_request_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    trader_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    status: Mapped[FollowRequestStatus] = mapped_column(
        Enum(FollowRequestStatus, name="follow_request_status",
             values_callable=lambda e: [m.value for m in e]),
        default=FollowRequestStatus.PENDING,
        server_default=FollowRequestStatus.PENDING.value,
        nullable=False, index=True,
    )
    # When the trader approved/rejected. NULL while pending.
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    subscriber = relationship("User", foreign_keys=[subscriber_id])
    trader = relationship("User", foreign_keys=[trader_id])
