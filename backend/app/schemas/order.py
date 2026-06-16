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

    # Bracket legs. Optional but at least one must be set for the order
    # to be routed as a bracket. Alpaca requires BOTH on a real bracket;
    # if only one is provided we still attach it (oto/oco semantics may
    # vary by adapter — Alpaca falls back to a single attached leg).
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)

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
        # Bracket sanity checks. Brackets attach exit legs to the parent
        # entry, so they only make sense on the entry types Alpaca accepts
        # (market / limit). Stop / stop-limit entries are themselves
        # exits — Alpaca rejects bracket on them.
        has_bracket = self.take_profit_price is not None or self.stop_loss_price is not None
        if has_bracket:
            if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
                raise ValueError(
                    "take_profit/stop_loss only supported on market/limit entries"
                )
            # Sane price relationship for BUY brackets: TP above entry,
            # SL below entry. SELL brackets (short) flip both. Validating
            # against the entry price catches obvious off-by-side mistakes
            # before the broker rejects them. We only enforce when both
            # bracket prices AND a reference entry price (limit) are set —
            # market entries don't have a known price up-front.
            ref = self.limit_price
            if ref is not None and self.take_profit_price and self.stop_loss_price:
                if self.side == OrderSide.BUY:
                    if not (self.stop_loss_price < ref < self.take_profit_price):
                        raise ValueError(
                            "buy bracket: stop_loss must be < limit < take_profit"
                        )
                else:
                    if not (self.take_profit_price < ref < self.stop_loss_price):
                        raise ValueError(
                            "sell bracket: take_profit must be < limit < stop_loss"
                        )
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
    # Nullable: orders survive when their broker is disconnected (SET NULL
    # at the DB level). See models/order.py for the rationale.
    broker_account_id: uuid.UUID | None
    instrument_type: InstrumentType
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    # Bracket-exit linkage — entry rows have these null; the TP / SL exit
    # rows placed by bracket_emulator point back at their parent and tag
    # themselves with 'tp' or 'sl'. The frontend uses these to filter
    # exit legs out of the "find entry for position" lookup.
    bracket_parent_id: uuid.UUID | None = None
    bracket_leg: str | None = None
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
    fanned_out_to_subscribers: bool = False
    fills: list[FillOut] = []

    model_config = {"from_attributes": True}


class DailyPnL(BaseModel):
    day: date
    realized_pnl: Decimal
    trade_count: int


class CloseOrderIn(BaseModel):
    """Close (reverse) a filled order. Quantity defaults to the original
    filled_quantity, but the trader can specify less for partial close."""

    order_type: OrderType = OrderType.MARKET   # market or limit
    limit_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "CloseOrderIn":
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price required for limit close")
        if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("close only supports market or limit")
        return self


class BracketUpdateIn(BaseModel):
    """Modify the TP/SL legs of an entry order's bracket.

    Either field may be:
      * a positive Decimal → set or replace that leg at this price
      * explicit ``None`` → clear that leg (cancel any live exit on
        that side; subsequent fills won't have a bracket on that leg)
      * field omitted entirely → leave that leg unchanged

    Pydantic can't distinguish "omitted" from "set to null" out of the
    box. We use ``Field(default=...)`` with a sentinel default and a
    ``model_validator`` that reads the raw input dict — that lets the
    endpoint tell the three cases apart.
    """

    take_profit_price: Decimal | None = Field(default=None)
    stop_loss_price: Decimal | None = Field(default=None)

    # Set by the validator: which keys were present in the request body
    # (regardless of value). Endpoint reads this to know what to act on.
    tp_present: bool = Field(default=False, exclude=True)
    sl_present: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _track_presence(cls, data):  # noqa: ANN001 — pydantic before-validator
        if isinstance(data, dict):
            data = dict(data)  # shallow copy so we don't mutate the caller's payload
            data["tp_present"] = "take_profit_price" in data
            data["sl_present"] = "stop_loss_price" in data
        return data

    @model_validator(mode="after")
    def _check(self) -> "BracketUpdateIn":
        if not self.tp_present and not self.sl_present:
            raise ValueError("at least one of take_profit_price / stop_loss_price required")
        if self.take_profit_price is not None and self.take_profit_price <= 0:
            raise ValueError("take_profit_price must be > 0")
        if self.stop_loss_price is not None and self.stop_loss_price <= 0:
            raise ValueError("stop_loss_price must be > 0")
        return self
