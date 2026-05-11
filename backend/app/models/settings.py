import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TraderSettings(Base, TimestampMixin):
    """One row per trader. Master kill switch for outgoing trades."""

    __tablename__ = "trader_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
    subscription_tier: Mapped[str] = mapped_column(String(40), default="basic", nullable=False)

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
