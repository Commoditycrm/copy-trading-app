import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric
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
    # ``copy_enabled`` flips to False. NULL = off. Re-enable is manual
    # only — the contract is "stop until I turn it back on", same as
    # every other limit. Stamped with ``auto_liquidated_at`` when the
    # trigger fires so the Settings page can show "Auto-liquidated at HH:MM".
    auto_liquidation_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )
    auto_liquidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Set to the UTC timestamp at which a DAILY limit (loss, profit, or
    # max_account_pct_per_day, plus their _pct variants) flipped
    # copy_enabled to False. NULL means "not paused by a daily limit" —
    # either the user manually disabled (we leave them alone), or copy is
    # currently enabled, or the pause already auto-resumed.
    #
    # Auto-resume: on every fanout entry (copy_engine) AND every pnl_poller
    # tick, if this timestamp is set AND its UTC date is < today's UTC
    # date, we flip copy_enabled back to True and clear this stamp. That's
    # how "auto-resume next UTC day" works for daily limits.
    #
    # Auto-liquidation (`auto_liquidation_limit`) deliberately uses a
    # DIFFERENT column (`auto_liquidated_at`) and is NEVER touched by the
    # auto-resume sweep — equity-floor liquidation stays sticky until the
    # subscriber manually re-enables copy. That's the intentional split:
    # daily limits forgive on the next day, hard-equity liquidation does
    # not.
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

    # Per-position TAKE-PROFIT / STOP-LOSS percentages, applied to every
    # open position the subscriber holds. Independent of any TP/SL on
    # the trader's mirrored entry (which subscribers no longer receive —
    # see copy_engine.fanout_async). pnl_poller checks each tick: for
    # every open position, computes `unrealized_pnl / abs(cost_basis) *
    # 100`. If >= position_tp_pct → close that position at market. If
    # <= -position_sl_pct → same. Per-position only — does NOT flip
    # copy_enabled (other positions and new mirrors keep flowing).
    # Numeric(7,2) so a position_tp_pct of 999.99 is representable
    # (1000%+ moonshots happen on options); SL is bounded 0 < pct <= 100
    # by the API layer since you can't lose more than 100% of cost.
    position_tp_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 2), nullable=True,
    )
    position_sl_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

    # When True, this subscriber COPIES the trader's per-trade SL/TP instead
    # of using their own position_tp_pct / position_sl_pct. copy_engine
    # records the trader's bracket as a percent distance on each mirrored
    # entry (Order.take_profit_pct / stop_loss_pct), and the bracket
    # emulator re-anchors it onto the subscriber's own fill when the entry
    # fills. While True, position_enforcer SKIPS this subscriber's own
    # per-position TP/SL so the two mechanisms can't double-close a
    # position. Default False preserves the prior behaviour (own per-
    # position TP/SL; trader's bracket stripped from mirrors).
    copy_trader_bracket: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
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
    # How many additional attempts to make after the original failure.
    # 1 = current behaviour (one retry), max 5. Only consulted when
    # retry_interval_open/close is not "never".
    retry_max_attempts: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False,
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
