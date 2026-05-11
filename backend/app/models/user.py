import enum
import uuid

from sqlalchemy import Enum, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    TRADER = "trader"
    SUBSCRIBER = "subscriber"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # SnapTrade identity (created lazily on first broker-connect attempt).
    # We use the User.id as the SnapTrade userId, so this column just records
    # whether registration succeeded; the secret is encrypted with Fernet.
    snaptrade_registered: Mapped[bool] = mapped_column(default=False, nullable=False)
    encrypted_snaptrade_user_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

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
