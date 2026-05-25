import enum
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class SubscriberSettingsOut(BaseModel):
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None
    todays_realized_pnl: Decimal | None = None  # populated by GET endpoint, not by PATCH responses
    # Retry policy for transient broker errors. "never" disables retry.
    # Sent as the bare enum string ("never"/"1m"/"2m"/"3m"/"5m") so the
    # frontend can render dropdowns without a separate mapping. Validator
    # coerces a passed-in enum member to its `.value` so the
    # response_model path (which auto-builds this from a SubscriberSettings
    # ORM row) doesn't end up with "RetryInterval.NEVER".
    retry_interval_open: str = "never"
    retry_interval_close: str = "never"

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


class RetryIntervalIn(BaseModel):
    """Subscriber-set retry policy. Either or both fields may be present —
    only the supplied ones are updated, the rest stay as-is. Valid values:
    "never", "1m", "2m", "3m", "5m"."""

    retry_interval_open: str | None = None
    retry_interval_close: str | None = None


class FollowTraderIn(BaseModel):
    trader_id: uuid.UUID | None  # null to unfollow


class TraderToggleIn(BaseModel):
    trading_enabled: bool


class SubscriberMultiplierIn(BaseModel):
    """Trader-only override of a subscriber's multiplier."""

    multiplier: Decimal = Field(gt=0, le=100)


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
