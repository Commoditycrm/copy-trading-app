import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class BrokerName(str, enum.Enum):
    """Brokers we directly integrate with. Adding a new one means writing an
    adapter under app/brokers/."""
    ALPACA = "alpaca"


class BrokerAccount(Base, TimestampMixin):
    """One connected brokerage account, owned by one app user.

    Credentials are stored encrypted (Fernet) in `encrypted_credentials` as a
    JSON blob whose shape depends on the broker. For Alpaca it's
    `{"api_key": "...", "api_secret": "...", "paper": true}`.
    """

    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # values_callable tells SQLAlchemy to send enum.value (e.g. "alpaca") instead
    # of enum.name ("ALPACA") to Postgres. The DB-side enum was created with the
    # lowercase value, so this keeps Python ↔ Postgres in sync.
    broker: Mapped[BrokerName] = mapped_column(
        Enum(BrokerName, name="broker_name",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_fractional: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Encrypted JSON blob. Decrypt via services.crypto.decrypt_json.
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)

    # Broker's own account number/id for display
    broker_account_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    connection_status: Mapped[str] = mapped_column(String(40), default="connected", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cached balance snapshot
    cash: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    buying_power: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="broker_accounts")
    orders = relationship("Order", back_populates="broker_account", cascade="all, delete-orphan")
