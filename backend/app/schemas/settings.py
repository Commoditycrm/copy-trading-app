import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class SubscriberSettingsOut(BaseModel):
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    # Brand of the trader being followed — surfaced in the shell so the
    # subscriber sees the trader's app name (not "ARK"). None when not
    # following anyone, or for legacy traders that pre-date business_name.
    following_trader_business_name: str | None = None
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None
    # Symmetric counterpart to daily_loss_limit. Positive amount — e.g. 500
    # means "auto-pause copy after $500 realized profit today". NULL = no
    # profit cap.
    daily_profit_limit: Decimal | None = None
    # Percentage variants — the new UI uses these. Each is a percent
    # (0 < x <= 100) of the broker's beginning-day balance. pnl_poller
    # derives the dollar threshold each tick and trips the same kill
    # switch when today's realized P&L breaches it.
    daily_loss_limit_pct: Decimal | None = None
    daily_profit_limit_pct: Decimal | None = None
    todays_realized_pnl: Decimal | None = None  # populated by GET endpoint, not by PATCH responses
    # UI-only — persisted but never enforced server-side.
    max_per_contract: Decimal | None = None
    # Per-position TP/SL percentages applied to every open position.
    # pnl_poller closes the offending position at market when the
    # unrealized P&L breaches the configured threshold. Independent of
    # the daily kill switches and of auto_liquidation_limit.
    position_tp_pct: Decimal | None = None
    position_sl_pct: Decimal | None = None
    # When True the subscriber copies the trader's per-trade SL/TP
    # (re-anchored onto their own fill) instead of using the per-position
    # TP/SL above. The two are mutually exclusive — see position_enforcer.
    copy_trader_bracket: bool = False
    # Percent of account equity (0–100). pnl_poller checks every 60s using
    # today's equity from Alpaca; pauses copy if today's P&L breaches the
    # derived dollar threshold.
    max_account_pct_per_day: Decimal | None = None
    # Account-equity floor that triggers full liquidation + copy disable.
    # When the pnl_poller sees broker equity <= this value, every open
    # position is closed at market and copy_enabled flips to False until
    # the subscriber manually re-enables it.
    auto_liquidation_limit: Decimal | None = None
    auto_liquidated_at: datetime | None = None
    # Retry policy for transient broker errors. "never" disables retry.
    # Sent as the bare enum string ("never"/"1m"/"2m"/"3m"/"5m") so the
    # frontend can render dropdowns without a separate mapping. Validator
    # coerces a passed-in enum member to its `.value` so the
    # response_model path (which auto-builds this from a SubscriberSettings
    # ORM row) doesn't end up with "RetryInterval.NEVER".
    retry_interval_open: str = "never"
    retry_interval_close: str = "never"
    # How many additional retry attempts after the original failure (1–5).
    # Consulted only when retry_interval_open/close != "never".
    retry_max_attempts: int = 1
    # Per-subscriber symbol filters. Both default to empty lists, which
    # means "no filter applied" (mirror every trade). See SubscriberSettings
    # model for the precedence rules.
    symbol_exclusion_list: list[str] = []
    symbol_inclusion_list: list[str] = []

    @field_validator("retry_interval_open", "retry_interval_close", mode="before")
    @classmethod
    def _enum_to_value(cls, v):
        if isinstance(v, enum.Enum):
            return v.value
        return v

    model_config = {"from_attributes": True}


class TraderSettingsOut(BaseModel):
    user_id: uuid.UUID
    trading_enabled: bool
    # False = subscribers must request + be approved to follow; True = anyone
    # can follow this trader directly ("auto-allow").
    auto_approve_follows: bool = False

    model_config = {"from_attributes": True}


class SubscriberToggleIn(BaseModel):
    copy_enabled: bool


class SubscriberSelfMultiplierIn(BaseModel):
    """Subscriber-editable multiplier. Bounded so a misclicked extra zero
    doesn't 100x someone's exposure."""

    multiplier: Decimal = Field(gt=0, le=10)


class DailyLossLimitIn(BaseModel):
    """Subscriber-set daily realized-loss kill switch. Pass null to disable."""

    daily_loss_limit: Decimal | None = Field(default=None, ge=0)


class DailyProfitLimitIn(BaseModel):
    """Subscriber-set daily realized-profit auto-pause. Pass null to disable.
    Symmetric to DailyLossLimitIn — both flip copy_enabled=False when hit,
    both auto-resume at the next UTC midnight via copy_engine."""

    daily_profit_limit: Decimal | None = Field(default=None, ge=0)


class MaxPerContractIn(BaseModel):
    """UI-only dollar ceiling per contract. Persisted but not enforced."""

    max_per_contract: Decimal | None = Field(default=None, ge=0)


class MaxAccountPctIn(BaseModel):
    """% of today's beginning-day account balance. When today's
    cumulative filled trade NOTIONAL (USD) crosses
    ``beginning_day_balance * pct/100``, pnl_poller auto-pauses copy.
    0 < pct <= 100."""

    max_account_pct_per_day: Decimal | None = Field(default=None, gt=0, le=100)


class DailyLossLimitPctIn(BaseModel):
    """Subscriber-set daily realized-loss kill switch — percentage
    variant. 0 < pct <= 100. pnl_poller derives the dollar threshold
    each tick from beginning_day_balance. Pass null to disable."""

    daily_loss_limit_pct: Decimal | None = Field(default=None, gt=0, le=100)


class DailyProfitLimitPctIn(BaseModel):
    """Symmetric to DailyLossLimitPctIn — daily realized-profit cap as
    a percentage of beginning-day balance. Pass null to disable."""

    daily_profit_limit_pct: Decimal | None = Field(default=None, gt=0, le=100)


class PositionTpPctIn(BaseModel):
    """Per-position take-profit % applied to EVERY open position.
    When `unrealized_pnl / abs(cost_basis) * 100 >= position_tp_pct`,
    pnl_poller closes that position at market. Pass null to disable.

    Ceiling is intentionally high (1000%) — options can multi-bag.
    Min is >0; a TP of 0% would close every position immediately."""

    position_tp_pct: Decimal | None = Field(default=None, gt=0, le=1000)


class PositionSlPctIn(BaseModel):
    """Per-position stop-loss % applied to EVERY open position.
    When `unrealized_pnl / abs(cost_basis) * 100 <= -position_sl_pct`,
    pnl_poller closes that position at market. Pass null to disable.

    Ceiling is 100% — you can't lose more than the cost basis.
    Min is >0; an SL of 0% would close every position immediately."""

    position_sl_pct: Decimal | None = Field(default=None, gt=0, le=100)


class AutoLiquidationLimitIn(BaseModel):
    """Subscriber-set hard floor on account equity. Pass null to disable.
    When broker-reported equity falls to/below this value, pnl_poller
    runs a full liquidation of the subscriber's open positions and flips
    copy_enabled=False until they manually re-enable."""

    auto_liquidation_limit: Decimal | None = Field(default=None, gt=0)


class CopyTraderBracketIn(BaseModel):
    """Toggle whether the subscriber copies the trader's per-trade SL/TP
    (re-anchored onto their own fill) instead of their own per-position
    TP/SL %. The two are mutually exclusive."""

    copy_trader_bracket: bool


class RetryIntervalIn(BaseModel):
    """Subscriber-set retry policy. Any field may be omitted — only the
    supplied ones are updated. Valid interval values: "never", "1m",
    "2m", "3m", "5m". retry_max_attempts must be 1–5."""

    retry_interval_open: str | None = None
    retry_interval_close: str | None = None
    retry_max_attempts: int | None = Field(default=None, ge=1, le=5)


class SymbolFilterIn(BaseModel):
    """Subscriber-set symbol filters. Either field may be omitted — only
    the supplied list replaces the stored one. Each list is capped at 200
    symbols so a runaway paste doesn't bloat the row. Symbols are
    uppercased + deduped server-side before persisting."""

    symbol_exclusion_list: list[str] | None = Field(default=None, max_length=200)
    symbol_inclusion_list: list[str] | None = Field(default=None, max_length=200)


class FollowTraderIn(BaseModel):
    trader_id: uuid.UUID | None  # null to unfollow


class TraderToggleIn(BaseModel):
    """Partial update of the trader's own settings. Any field left unset is
    unchanged, so the master-switch toggle and the follow-policy dropdown can
    each PATCH just their field."""
    trading_enabled: bool | None = None
    auto_approve_follows: bool | None = None


class SubscriberMultiplierIn(BaseModel):
    """Trader-only override of a subscriber's multiplier."""

    multiplier: Decimal = Field(gt=0, le=100)


class SubscriberBulkRemoveIn(BaseModel):
    """Trader-side bulk unfollow.

    Each id should be a SubscriberSettings.user_id that currently has
    following_trader_id = the calling trader's id. IDs that don't match
    are silently skipped so the caller doesn't have to filter perfectly
    before sending — partial-success is the right ergonomic for bulk UI.
    """

    subscriber_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class BulkCopyStateOut(BaseModel):
    """`total`/`enabled` reflect subscribers' own copy flags (informational).
    `paused` is the trader-side master fanout gate — when True, no mirrors
    are placed regardless of subscribers' individual settings."""

    total: int
    enabled: int
    paused: bool = False


class BulkCopyToggleIn(BaseModel):
    enabled: bool


class SubscriberSummary(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    copy_enabled: bool
    multiplier: Decimal
    broker_count: int
    realized_pnl_30d: Decimal
