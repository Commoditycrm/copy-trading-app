"""Multi-trader following: one row per (subscriber, trader) actively followed.

Replaces the single ``SubscriberSettings.following_trader_id`` as the source of
truth for *who a subscriber mirrors*, so one subscriber can follow MANY traders
at once. Copy settings stay **global** on ``SubscriberSettings`` (one multiplier /
one on-off / one filter set applied to every followed trader) — this table only
records the follow relationships.

``following_trader_id`` is kept as the subscriber's *primary* trader (for the app
wordmark and legacy single-trader lookups); the fanout reads THIS table so a
trade fans out to every subscriber who follows that trader.

Distinct from ``follow_requests`` (which grants *permission* to follow): a row
here means the subscriber has actively chosen to follow that approved trader.
"""
import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SubscriberFollow(Base, TimestampMixin):
    __tablename__ = "subscriber_follows"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "trader_id", name="uq_subscriber_follow_pair"),
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
