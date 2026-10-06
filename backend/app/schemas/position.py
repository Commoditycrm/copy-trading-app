"""Schemas for /api/positions — currently held positions at the broker."""
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.order import InstrumentType, OptionRight, OrderType


class ProtectionOut(BaseModel):
    """One thing protecting a position — see services/position_protections."""
    kind: str                         # "stop" | "trailing_stop" | "take_profit"
    price: str | None = None          # the level now (a trailing stop's current stop)
    quantity: str | None = None       # contracts / shares covered; None = the whole position
    where: str = "app"                # the broker it rests at ("Webull", "Alpaca"), or "app"
    order_id: str | None = None       # the resting order, when there is one
    source: str = "order"             # "ladder" | "order" | "bracket"
    note: str | None = None           # e.g. "Trim 2", "linked to the take-profit"
    trail_pct: str | None = None
    trail_amount: str | None = None
    peak: str | None = None           # the high a trailing stop is measured from


class PositionOut(BaseModel):
    broker_account_id: uuid.UUID
    broker_symbol: str                # canonical broker id (OCC for options, ticker for stocks)
    symbol: str                       # bare ticker (root for options)
    instrument_type: InstrumentType
    quantity: Decimal                 # signed: positive = long, negative = short
    avg_entry_price: Decimal | None
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None            # Open P&L ($) — broker's own value
    cost_basis: Decimal | None
    # Broker-native per-position figures, following the connected broker (Webull
    # / Alpaca) — never derived to imitate another broker. Percents are display
    # percents (e.g. -39.16). day_pnl / day_pnl_pct are the CURRENT trading day
    # only; None when the broker doesn't expose them. day_pnl_source names the
    # provenance so the UI can show it as broker-authoritative.
    open_pnl_pct: Decimal | None = None
    day_pnl: Decimal | None = None
    day_pnl_pct: Decimal | None = None
    day_pnl_source: str | None = None         # "broker_native" | None
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
    # The Discord exit ladder's stop level on this position, if it has one.
    # That stop is its own order at the broker (not the entry's bracket SL), so
    # the row needs to know about it to offer "Cancel stop" for it.
    ladder_stop_price: Decimal | None = None
    # Stops, trailing stops and take-profits on this position (Positions page
    # icons + their details).
    protections: list[ProtectionOut] = []


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


class StaleAccount(BaseModel):
    """A broker account whose positions ARE listed, but from a recent snapshot:
    the live read was rate-limited (Webull 429). Shown with its age rather than
    leaving the account out."""
    broker_account_id: uuid.UUID
    broker: str
    label: str | None = None
    age_s: int


class PositionsPayload(BaseModel):
    """Detailed form of GET /api/positions (``?detail=1``).

    The bare-list form stays the default so existing callers are untouched;
    only the UI that needs to SAY something about a failure opts in."""
    positions: list[PositionOut]
    unreachable: list[UnreachableAccount] = []
    stale: list[StaleAccount] = []


class PositionChannelIn(BaseModel):
    """Assign a held position to a Discord channel (Positions → Channel).

    ``channel`` is a channel's id, "self" for the trader's own Self channel, or
    "auto" to go back to the channel whose alert opened the position.
    ``entry_price`` is the position's average cost as the page shows it — used
    only to start an exit ladder on a position that has none yet.
    """

    symbol: str = Field(min_length=1, max_length=40)
    option_strike: Decimal | None = None
    option_right: OptionRight | None = None
    option_expiry: date | None = None
    channel: str = Field(min_length=1, max_length=40)
    entry_price: Decimal | None = Field(default=None, gt=0)


class AveragePositionIn(BaseModel):
    """Add to an open position (average it) at market or at a limit."""

    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> "AveragePositionIn":
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price required for a limit average")
        if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("order_type must be market or limit")
        return self


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
