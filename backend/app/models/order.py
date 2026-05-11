import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"        # accepted by us, not yet sent
    SUBMITTED = "submitted"    # sent to broker
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class InstrumentType(str, enum.Enum):
    STOCK = "stock"
    OPTION = "option"


class OptionRight(str, enum.Enum):
    CALL = "call"
    PUT = "put"


class Order(Base, TimestampMixin):
    """Represents a single order at one broker account.

    For a trader's order, parent_order_id is NULL.
    For a mirrored order on a subscriber's account, parent_order_id points to the trader's order.
    """

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )

    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    # Option-only fields. NULL for stock orders.
    option_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    option_right: Mapped[OptionRight | None] = mapped_column(
        Enum(OptionRight, name="option_right"), nullable=True
    )

    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide, name="order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType, name="order_type"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), default=OrderStatus.PENDING, nullable=False, index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    filled_avg_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    broker_account = relationship("BrokerAccount", back_populates="orders")
    fills = relationship("Fill", back_populates="order", cascade="all, delete-orphan")
    parent = relationship("Order", remote_side=[id], backref="children")


class Fill(Base):
    """Individual execution against an Order. Source of truth for realized P&L."""

    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0, nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    order = relationship("Order", back_populates="fills")
