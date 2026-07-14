import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    TRADER = "trader"
    SUBSCRIBER = "subscriber"
    ADMIN = "admin"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Trader-only: shown as the app name across the shell for the trader and
    # for any subscriber who follows them. Required at registration for
    # role=trader, nullable here so existing rows + subscriber rows are valid.
    business_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Email verification (soft-enforced): unverified users can still log in,
    # but the app nags them with a banner until they confirm. Existing rows
    # were grandfathered to True by the migration.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # SMS notifications (opt-in). phone is E.164 ("+15551234567"); nullable since
    # most users won't provide one. sms_notifications_enabled gates the
    # notification→SMS fanout in services/notifications.py — off by default so we
    # never text anyone who hasn't consented.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sms_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    broker_accounts = relationship(
        "BrokerAccount", back_populates="user", cascade="all, delete-orphan"
    )
    subscriber_settings = relationship(
        "SubscriberSettings",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="SubscriberSettings.user_id",
    )
    trader_settings = relationship(
        "TraderSettings", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
