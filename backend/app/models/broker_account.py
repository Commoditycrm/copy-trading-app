import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class BrokerAccount(Base, TimestampMixin):
    """One row per brokerage account connected via SnapTrade.

    SnapTrade is the only broker integration layer; we don't store broker
    credentials directly. The snaptrade_account_id is the canonical identifier
    used for all subsequent API calls (orders, positions, balances).
    """

    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Broker name as reported by SnapTrade (e.g. "ALPACA", "SCHWAB", "WEBULL").
    # Free-form string — different brokers come and go on SnapTrade's platform.
    broker: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_fractional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    snaptrade_account_id: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False, index=True
    )
    broker_account_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    connection_status: Mapped[str] = mapped_column(String(40), default="connected", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Balance snapshot — refreshed on broker sync or via the refresh-balance endpoint.
    # Nullable because we may not have polled yet; balance_updated_at = NULL signals "never fetched".
    cash: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    buying_power: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="broker_accounts")
    orders = relationship("Order", back_populates="broker_account", cascade="all, delete-orphan")
