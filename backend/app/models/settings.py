import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID
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

    # Daily realized-PROFIT kill switch — symmetric counterpart to
    # daily_loss_limit. Positive amount (e.g. 500 = "stop after $500 profit
    # today"). NULL disables. When today's realized P&L reaches
    # +daily_profit_limit, copy_enabled flips to false (same path as loss).
    daily_profit_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Percentage variants of the loss / profit kill switches — the UI
    # uses these now and the absolute USD columns above are legacy. Each
    # is a percent of the broker's beginning-day balance. pnl_poller
    # computes the dollar threshold each tick as
    # ``beginning_day_balance * pct / 100`` and trips the kill switch on
    # the same realized-P&L breach. Bounds: 0 < pct <= 100. NULL = off.
    daily_loss_limit_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )
    daily_profit_limit_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

    # Account-equity floor that triggers FULL LIQUIDATION + copy disable.
    # When the pnl_poller observes broker-reported equity <= this value,
    # everything on the subscriber's broker is closed at market AND
    # ``copy_enabled`` flips to False. Unlike the daily limits, copy does
    # NOT auto-resume next day — the subscriber has to manually re-enable
    # (that's the contract: "stop until I turn it back on"). NULL = off.
    # Stamped with ``auto_liquidated_at`` when the trigger fires so the
    # Settings page can show "Auto-liquidated at HH:MM".
    auto_liquidation_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )
    auto_liquidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Set to the UTC timestamp at which a P&L-limit (loss OR profit) flipped
    # copy_enabled to False. NULL means "not paused by a limit" — either the
    # user manually disabled (we leave them alone), or copy is currently
    # enabled. On every fanout entry, if this timestamp is set AND its UTC
    # date is < today's UTC date, copy_engine clears it and re-enables
    # copy_enabled — that's how "auto-resume next day" works.
    pnl_auto_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # UI-only ceiling on per-contract dollar size — surfaced in the Settings
    # panel for the user to track their own risk, NOT enforced server-side.
    # We persist it so the value survives refresh and round-trips through
    # PATCH/GET like the other limits.
    max_per_contract: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Percentage of TODAY'S BEGINNING-DAY ACCOUNT BALANCE (Alpaca's
    # ``last_equity`` — equity at yesterday's close) that bounds today's
    # cumulative filled trade NOTIONAL (capital deployed in mirror orders
    # today, both buy + sell, options × 100). When notional crosses
    # ``beginning_day_balance * pct/100``, copy is auto-paused. Stored as
    # the percent value itself (e.g. 50.00 = 50%). Enforced by pnl_poller
    # every 60s. Using day-start balance (not live equity) keeps the
    # dollar threshold FIXED for the trading day — it doesn't drift up
    # on gains or down on losses mid-day. NULL = feature disabled.
    max_account_pct_per_day: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

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

    # Per-subscriber symbol filters. Both stored as JSONB arrays of
    # uppercase tickers ("AAPL", "TSLA"). copy_engine consults these on
    # every fanout:
    #   - exclusion_list non-empty + trader's symbol IN it  → skip mirror
    #   - inclusion_list non-empty + trader's symbol NOT in → skip mirror
    #   - empty lists                                       → mirror everything
    # Defaults are empty so existing subscribers' behaviour is unchanged
    # after the migration.
    symbol_exclusion_list: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False,
    )
    symbol_inclusion_list: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False,
    )

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
