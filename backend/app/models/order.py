import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String
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
    # Broker rejected the order with a transient error (5xx / 429 / timeout /
    # connection reset). retry_scheduler will pick this up at retry_at and
    # try once more; on success → SUBMITTED, on failure → REJECTED.
    RETRY_PENDING = "retry_pending"


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
    # Nullable + ON DELETE SET NULL on purpose. When a user disconnects a
    # broker, we DO NOT want to lose every Order tied to it (that would wipe
    # the Performance / Order History audit trail). The previous CASCADE was
    # a silent footgun — see the broker-reconnect-clears-performance
    # incident. Orders whose broker was later removed survive with this
    # column NULL; they stay visible in history but cancel/close endpoints
    # 404 since there's no broker to talk to any more.
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
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

    # Bracket-order legs attached to a parent entry. Both NULL = plain
    # order; both set = bracket (Alpaca OrderClass.BRACKET on supported
    # brokers). Mirror children inherit these prices verbatim — multipliers
    # only scale quantity, not price levels.
    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Percent-distance bracket — used for COPIED subscriber brackets only.
    # When a subscriber opts into "copy trader's SL/TP"
    # (SubscriberSettings.copy_trader_bracket), copy_engine records the
    # trader's TP/SL as a positive percent distance from the trader's entry
    # here (NOT the absolute price — the subscriber may fill at a different
    # price). The bracket emulator re-anchors these onto the subscriber's
    # OWN fill (limit_price first, else filled_avg_price) when the entry
    # fills, so every subscriber gets the same risk/reward % regardless of
    # their fill or multiplier. Sign convention matches the frontend's
    # InlineBracketCell: the stored value is the positive distance, and the
    # emulator applies the leg/side direction. NULL on the trader's own
    # orders and on subscriber orders when the toggle is off (those use the
    # absolute *_price columns above, or no bracket at all).
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)

    # Linkage for *emulated* bracket exits on adapters without native OCO
    # (everything except Alpaca direct). When the entry fills, the
    # `bracket_emulator` service places TP (LIMIT) + SL (STOP) on the
    # opposite side; those exit Order rows reference back via
    # ``bracket_parent_id`` and identify themselves with
    # ``bracket_leg in {'tp','sl'}``. Both stay NULL on a regular order or
    # an Alpaca-native bracket entry (Alpaca brackets it server-side and
    # the exit legs never become rows in our DB).
    bracket_parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    bracket_leg: Mapped[str | None] = mapped_column(String(4), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), default=OrderStatus.PENDING, nullable=False, index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    filled_avg_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ── Copy-trade pipeline lifecycle timestamps (Performance page) ──────
    # All nullable; parent-only fields are NULL on child rows and vice versa.
    # Filled by trades.py, trade_listener.py, copy_engine.py, services/events.py
    # at the corresponding step. See alembic migration e7a1d2c40f01 for the
    # field-by-field meanings.

    # Parent-only:
    trader_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    socket_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Both parent and child (set when the SSE event for the order is published):
    redis_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Child-only:
    subscriber_picked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscriber_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    broker_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Duration (ms) of the subscriber's broker place-order REST call —
    # request → response, capturing BOTH a success and an error response.
    # Measured around the SDK call in copy_engine fanout (Phase 2).
    broker_call_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Retry policy (transient broker errors) ──────────────────────────
    # Set on a child order whose broker call returned a transient error
    # (5xx, 429, timeout, connection reset). The retry_scheduler picks
    # rows up where retry_at <= now() and tries again, up to
    # subscriber_settings.retry_max_attempts times. is_closing
    # distinguishes opening vs closing intent so the subscriber's
    # open/close retry interval can be applied.
    retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Counts how many retry attempts have been made so far (0 = none yet).
    # Replaces the old boolean retry_attempted; supports 1-5 retries.
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    is_closing: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # True when this order was broadcast to subscribers via the copy-engine
    # fanout. False for: subscriber-owned orders, trader orders placed while
    # copy was paused, and orders placed with skip_fanout (e.g. Exit All "Just
    # me" scope). Powers the "My Orders" tab in Order History.
    fanned_out_to_subscribers: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    broker_account = relationship("BrokerAccount", back_populates="orders")
    fills = relationship("Fill", back_populates="order", cascade="all, delete-orphan")
    # `foreign_keys` is required now that there are TWO self-referential
    # FKs on `orders` (parent_order_id for copy-fanout linkage and
    # bracket_parent_id for emulated bracket exits). Without this the
    # mapper can't decide which column to join on and bails with
    # "multiple foreign key paths" at first query time.
    parent = relationship(
        "Order",
        remote_side=[id],
        foreign_keys=[parent_order_id],
        backref="children",
    )


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
