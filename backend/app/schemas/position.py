"""Schemas for /api/positions — currently held positions at the broker."""
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.order import InstrumentType, OptionRight, OrderType


class PositionOut(BaseModel):
    broker_account_id: uuid.UUID
    broker_symbol: str                # canonical broker id (OCC for options, ticker for stocks)
    symbol: str                       # bare ticker (root for options)
    instrument_type: InstrumentType
    quantity: Decimal                 # signed: positive = long, negative = short
    avg_entry_price: Decimal | None
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    cost_basis: Decimal | None
    option_expiry: date | None
    option_strike: Decimal | None
    option_right: OptionRight | None


class ClosePositionIn(BaseModel):
    """Close (or partially close) an open position by placing a reverse-side
    order. Quantity defaults to the full position size."""

    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "ClosePositionIn":
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price required for limit close")
        if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("close only supports market or limit")
        return self
