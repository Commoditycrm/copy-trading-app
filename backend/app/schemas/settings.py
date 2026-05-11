import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class SubscriberSettingsOut(BaseModel):
    user_id: uuid.UUID
    following_trader_id: uuid.UUID | None
    copy_enabled: bool
    multiplier: Decimal
    subscription_tier: str

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


class FollowTraderIn(BaseModel):
    trader_id: uuid.UUID | None  # null to unfollow


class TraderToggleIn(BaseModel):
    trading_enabled: bool


class SubscriberMultiplierIn(BaseModel):
    """Trader-only — set a subscriber's multiplier (subscription tier change)."""

    multiplier: Decimal = Field(gt=0, le=100)
    subscription_tier: str = Field(min_length=1, max_length=40)


class SubscriberSummary(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    copy_enabled: bool
    multiplier: Decimal
    subscription_tier: str
    broker_count: int
    realized_pnl_30d: Decimal
