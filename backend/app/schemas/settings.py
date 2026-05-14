import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class SubscriberSettingsOut(BaseModel):
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    copy_enabled: bool
    multiplier: Decimal
    daily_loss_limit: Decimal | None
    todays_realized_pnl: Decimal | None = None  # populated by GET endpoint, not by PATCH responses

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


class FollowTraderIn(BaseModel):
    trader_id: uuid.UUID | None  # null to unfollow


class TraderToggleIn(BaseModel):
    trading_enabled: bool


class SubscriberMultiplierIn(BaseModel):
    """Trader-only override of a subscriber's multiplier."""

    multiplier: Decimal = Field(gt=0, le=100)


class BulkCopyStateOut(BaseModel):
    """Aggregate copy_enabled state across a trader's subscribers.
    `enabled` = how many have copy on; `total` = how many follow this trader."""

    total: int
    enabled: int


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
