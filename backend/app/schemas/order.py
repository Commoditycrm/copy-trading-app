import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType


class PlaceOrderIn(BaseModel):
    instrument_type: InstrumentType
    symbol: str = Field(min_length=1, max_length=40)
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)

    # Required when instrument_type == OPTION
    option_expiry: date | None = None
    option_strike: Decimal | None = Field(default=None, gt=0)
    option_right: OptionRight | None = None

    @model_validator(mode="after")
    def _check(self) -> "PlaceOrderIn":
        if self.instrument_type == InstrumentType.OPTION:
            if not (self.option_expiry and self.option_strike and self.option_right):
                raise ValueError("option orders require expiry, strike, and right")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError("limit_price required for limit/stop_limit orders")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError("stop_price required for stop/stop_limit orders")
        return self


class FillOut(BaseModel):
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime

    model_config = {"from_attributes": True}


class OrderOut(BaseModel):
    id: uuid.UUID
    parent_order_id: uuid.UUID | None
    broker_account_id: uuid.UUID
    instrument_type: InstrumentType
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    option_expiry: date | None
    option_strike: Decimal | None
    option_right: OptionRight | None
    status: OrderStatus
    broker_order_id: str | None
    filled_quantity: Decimal
    filled_avg_price: Decimal | None
    submitted_at: datetime | None
    closed_at: datetime | None
    reject_reason: str | None
    created_at: datetime
    fills: list[FillOut] = []

    model_config = {"from_attributes": True}


class DailyPnL(BaseModel):
    day: date
    realized_pnl: Decimal
    trade_count: int
