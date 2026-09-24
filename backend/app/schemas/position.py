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
    # Reference price: the previous session's official market CLOSE for this
    # symbol (Alpaca previous_daily_bar). Lets the user compare the live price to
    # yesterday's close. None for options / when unavailable.
    reference_price: Decimal | None = None
    option_expiry: date | None
    option_strike: Decimal | None
    option_right: OptionRight | None
    # Which Discord channel's alert OPENED this position, matched by contract
    # against the most recent Discord entry. None for positions opened any
    # other way — trade panel, copy mirror, or bought in the broker's own app.
    discord_channel: str | None = None


class UnreachableAccount(BaseModel):
    """A broker account whose positions could NOT be read this request.

    Exists so the caller can tell "this account holds nothing" from "we could
    not ask". Those used to be indistinguishable: a failed read was swallowed
    and the endpoint returned 200 with the account simply absent, which the UI
    rendered as a flat account. On 2026-09-21 that showed subscribers an empty
    positions table whenever Webull answered 429 — they appeared to hold
    nothing while holding real positions."""
    broker_account_id: uuid.UUID
    broker: str
    label: str | None = None
    # Short, user-safe reason. Never the raw exception: it can carry ids.
    detail: str


class PositionsPayload(BaseModel):
    """Detailed form of GET /api/positions (``?detail=1``).

    The bare-list form stays the default so existing callers are untouched;
    only the UI that needs to SAY something about a failure opts in."""
    positions: list[PositionOut]
    unreachable: list[UnreachableAccount] = []


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
