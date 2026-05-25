import enum
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RetryInterval(str, enum.Enum):
    """How long to wait before retrying a transient-failed mirror order.
    NEVER disables retry entirely — failed orders go straight to REJECTED
    just like before this feature existed (no behaviour change)."""

    NEVER = "never"
    ONE_M = "1m"
    TWO_M = "2m"
    THREE_M = "3m"
    FIVE_M = "5m"


class TraderSettings(Base, TimestampMixin):
    """One row per trader. Master kill switch for outgoing trades."""

    __tablename__ = "trader_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Pause fanout to subscribers. Pure gate — subscribers' own copy_enabled
    # flags are NOT touched when this flips. When True, fanout skips everyone
    # regardless of their preference.
    copy_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="trader_settings")


class SubscriberSettings(Base, TimestampMixin):
    """One row per subscriber. Holds the multiplier, the trader being followed,
    and the subscriber-side kill switch."""

    __tablename__ = "subscriber_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    following_trader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("1.000"), nullable=False)

    # Daily realized-loss kill switch. Stored as a positive amount (e.g. 500 means
    # "stop after $500 loss today"). NULL disables the feature.
    # When today's realized P&L falls below -daily_loss_limit, copy_enabled is
    # auto-flipped to false and an audit + SSE event are emitted.
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Retry policy for transient broker errors. Two separate intervals so a
    # subscriber can be aggressive about closing positions (late close hurts
    # P&L) and conservative about opening (late open is usually fine — skip
    # the trade rather than enter at a worse price). NEVER → no retry, the
    # order goes straight to REJECTED on broker error (pre-retry behaviour).
    retry_interval_open: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval"),
        default=RetryInterval.NEVER, server_default="never", nullable=False,
    )
    retry_interval_close: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval"),
        default=RetryInterval.NEVER, server_default="never", nullable=False,
    )

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
