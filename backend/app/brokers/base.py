"""Broker adapter interface.

Every broker implementation conforms to this so the copy engine doesn't care which
one it's talking to. All methods are sync for now; switch to async if a broker SDK
forces it. Side effects (HTTP calls) belong here, not in API routes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType


@dataclass(frozen=True)
class BrokerCapabilities:
    """What a broker's API can authoritatively tell us about P&L. Core P&L logic
    branches on these, NOT on broker names — so adding a broker is a matter of
    declaring its capabilities, not editing pnl.py. Every flag defaults False:
    an undeclared broker is treated as exposing nothing, which is the safe side
    (we never fabricate confident numbers from data we don't have)."""

    # A COMPLETE, authoritative record of every execution exists via the broker's
    # API (an activity/fills feed). Only then may we FIFO realized P&L AND infer
    # that a lot still open past expiry truly expired worthless. Without it, a
    # remaining open lot is more likely a close we simply never received than a
    # real expiry — see the Webull phantom-loss finding.
    authoritative_fill_history: bool = False
    # Broker exposes a per-day historical P&L series we can pull (e.g. Alpaca
    # portfolio-history). False → historical days can't be reproduced exactly.
    historical_daily_pnl: bool = False
    # Broker reports today's live Day's P&L directly (a real API field).
    live_daily_pnl: bool = False
    # Broker reports current open-position unrealized authoritatively.
    authoritative_open_pnl: bool = False
    # Broker exposes a native per-position DAY P&L field (Webull day_profit_loss,
    # Alpaca unrealized_intraday_pl) — show the broker's number, not a derived one.
    authoritative_position_day_pnl: bool = False
    # Broker exposes an authoritative realized-P&L figure via a real API field
    # (NOT our FIFO inference). Only set when verified against an actual field.
    authoritative_realized_pnl: bool = False
    # Broker exposes a marked portfolio-history series (Alpaca).
    portfolio_history: bool = False
    # Broker PUSHES the authoritative ACCOUNT Day P&L metric to us directly (a
    # stream that carries account equity / Day P&L, not just order events).
    # False for every broker we integrate today: Alpaca's TradingStream and our
    # order SSE are an immediate REFRESH TRIGGER, not the P&L source itself, so
    # the account figure is always a polled GET. Keep False unless a real
    # account-P&L push exists.
    account_pnl_push: bool = False
    # Steady client refresh interval (seconds) for the account Day P&L surfaces
    # (calendar today cell + top card). Chosen per broker from its rate limits:
    # Alpaca 10s (200 req/min per-account key, ample headroom), Webull 30s (its
    # 2-reads/2s window gives no room to poll faster). The frontend reads this
    # from broker metadata rather than branching on broker names.
    recommended_refresh_interval_s: int = 30


@dataclass(frozen=True)
class ConnectionInfo:
    broker_account_id: str | None
    supports_fractional: bool
    extra: dict[str, Any]


@dataclass(frozen=True)
class BrokerOrderRequest:
    instrument_type: InstrumentType
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    option_expiry: date | None = None
    option_strike: Decimal | None = None
    option_right: OptionRight | None = None
    client_order_id: str | None = None
    # Open vs. close intent. Stock adapters (Alpaca) ignore it; SnapTrade's
    # options API needs it to pick BUY_TO_OPEN/SELL_TO_CLOSE etc.
    is_closing: bool = False
    # Route this order to the pre/post-market session. Alpaca ONLY fills
    # pre/post-market when this is True AND the order is a LIMIT (a plain
    # market order can't trade in extended hours). Ignored by brokers that
    # trade extended hours natively (Webull via SnapTrade).
    extended_hours: bool = False
    # Bracket-order exit legs attached to the parent entry. When either is
    # set on a market/limit entry, adapters that support bracket orders
    # (Alpaca) route through OrderClass.BRACKET; adapters that don't
    # support brackets fall through to a plain order and log a warning.
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    # Trailing-stop trail (order_type == TRAILING_STOP). Exactly one is set:
    # trail_percent (e.g. Decimal("5") = 5%) OR trail_price (a fixed dollar
    # trail). Only meaningful for adapters with supports_trailing_stop = True.
    trail_percent: Decimal | None = None
    trail_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerOrderLeg:
    """A child order of a native bracket (the take-profit / stop-loss legs
    Alpaca creates alongside the entry). Surfaced so the copy engine can
    materialise them as visible mirror rows for the subscriber."""
    broker_order_id: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerOrderResult:
    broker_order_id: str
    status: OrderStatus
    submitted_at: datetime
    filled_quantity: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    # The BROKER's own execution timestamp, when it reports one. None otherwise
    # — never substitute our clock here, because the entire point of the field
    # is to be distinguishable from the moment we noticed (Order.closed_at).
    filled_at: datetime | None = None
    reject_reason: str | None = None
    # Child legs of a native bracket entry (empty for plain orders).
    bracket_legs: tuple[BrokerOrderLeg, ...] = ()


@dataclass(frozen=True)
class BrokerPosition:
    """Snapshot of one held position at the broker. Quantity is signed:
    positive = long, negative = short.

    `broker_symbol` is the broker's canonical id for the position (e.g. the
    OCC symbol for options, plain ticker for stocks) — use it whenever you
    need a unique key. `symbol` is the human-friendly root for display."""

    broker_symbol: str
    symbol: str
    instrument_type: InstrumentType
    quantity: Decimal
    avg_entry_price: Decimal | None
    current_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    cost_basis: Decimal | None = None
    # Open P&L % — the broker's own lifetime unrealized return on this position,
    # as a PERCENT (e.g. -39.16), None when the broker doesn't expose it.
    open_pnl_pct: Decimal | None = None
    # Day's P&L — the position's P&L for the CURRENT trading day (not lifetime),
    # straight from the broker's native field (Webull day_profit_loss, Alpaca
    # unrealized_intraday_pl). None when the broker doesn't expose it — never
    # fabricated from the lifetime figure.
    day_pnl: Decimal | None = None
    # Day's P&L % — as a PERCENT. Native for Alpaca (unrealized_intraday_plpc);
    # derived for Webull (day_pnl / day-start value). None when unavailable.
    day_pnl_pct: Decimal | None = None
    # Option-only fields parsed from OCC symbol; null for stocks.
    option_expiry: date | None = None
    option_strike: Decimal | None = None
    option_right: OptionRight | None = None


class BrokerAdapter(ABC):
    """One instance per BrokerAccount. Hold decrypted credentials in-memory only."""

    name: str

    # What this broker's API can authoritatively report. Concrete adapters
    # override with their own; the default declares nothing (safe). Also exposed
    # name-keyed via app.brokers.capabilities.capabilities_for for callers that
    # only hold a BrokerName (e.g. pnl.py) and shouldn't build an adapter.
    capabilities: "BrokerCapabilities" = BrokerCapabilities()

    def __init__(self, credentials: dict[str, Any]):
        self.credentials = credentials

    @abstractmethod
    def verify_connection(self) -> ConnectionInfo:
        """Hit a lightweight authenticated endpoint. Raise on failure with a
        message suitable for surfacing to the user."""

    @abstractmethod
    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult: ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrderResult: ...

    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a working order.

        Returns True when we actually cancelled a live order, False when the
        broker reports it was ALREADY terminal (filled / cancelled / expired) —
        i.e. there was nothing to cancel.

        That distinction matters to anything doing cancel-then-replace: a False
        means the order may have FILLED, so placing a replacement would double
        the position. Callers that only want the end state ("no longer working")
        can keep ignoring the return value — both outcomes satisfy that.

        Raises only when the cancel genuinely failed and the order's state is
        unknown."""
        raise NotImplementedError

    # Whether this adapter can modify a working order's price/quantity IN PLACE
    # (an atomic broker-side replace) rather than cancel-then-place. In-place
    # replace never releases the position's share reservation, so a rapid
    # re-price can't race the release — the Alpaca cancel+replace failure seen on
    # prod (STKH, 2026-07-28): the cancelled order still showed the shares as
    # held_for_orders, so the immediate re-place was rejected "insufficient qty".
    # Adapters that support it set this True and implement replace_order; the
    # copy-engine modify path checks the flag and falls back to cancel+place.
    supports_replace: bool = False

    # Whether this broker REFUSES to trade a plain MARKET order in the pre/post
    # market session, so a mirror that must fill during extended hours has to be
    # re-routed as an explicitly-flagged marketable LIMIT instead.
    #
    # True for the direct integrations that gate the session at the order level:
    #   * Alpaca — extended hours needs order_type=LIMIT + extended_hours=True;
    #   * Webull direct — a MARKET order is forced to support_trading_session
    #     CORE (see WebullAdapter._session), so it just queues until 09:30.
    # False for aggregator-routed accounts (SnapTrade), where the upstream broker
    # handles the session itself and a MARKET order trades extended hours
    # natively — re-routing those to a limit would only make them miss.
    #
    # Consumed by copy_engine._needs_extended_hours_limit, which pairs it with
    # the current clock. Default False so a broker that hasn't been checked is
    # never handed an extended-hours order it can't honour.
    requires_extended_hours_limit: bool = False

    # Whether this adapter can place a native TRAILING_STOP order. The Sell-All /
    # close flow checks this per position: when True (and the instrument is
    # eligible — most brokers offer trailing stops on stocks only), it closes the
    # position with a trailing stop; when False it falls back to a plain
    # market/limit close. See services.trailing_stop_close. Default False so a
    # broker that hasn't implemented it is never handed a trailing stop.
    supports_trailing_stop: bool = False

    def replace_order(self, broker_order_id: str, req: BrokerOrderRequest) -> BrokerOrderResult:
        """Modify a WORKING order's price/quantity in place, atomically, returning
        the resulting (replacement) order. Only defined for adapters with
        ``supports_replace = True`` — the caller checks the flag first.

        Atomic contract: on FAILURE the ORIGINAL order is left untouched (still
        working at its old terms), so a failed replace never strands the
        subscriber without an order. On success the broker may return a NEW order
        id for the replacement (Alpaca does)."""
        raise NotImplementedError

    def get_positions(self, *, cached_ok: bool = False) -> list[BrokerPosition]:
        """List currently held positions at this broker account.

        ``cached_ok`` lets a DISPLAY caller accept a very recent cached read so
        that simultaneous readers share one request. Adapters are free to ignore
        it; only Webull implements it today, because Webull is the only broker
        that rejects concurrent position reads (429). Callers that decide
        whether to PLACE an order must leave it False."""
        raise NotImplementedError

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """Polled by ``services.pnl_poller`` every 5s to drive the daily
        P&L tile and the day-start-balance-based pct kill switch.

        Returns ``{"todays_pl", "equity", "beginning_day_balance"}`` —
        all Decimals — or ``None`` on failure. ``beginning_day_balance``
        may itself be None for broker integrations that don't surface a
        day-start figure (some SnapTrade brokers); the poller falls back
        to no pct enforcement for that subscriber when that's the case.

        Adapters that haven't implemented this default to None — the
        poller skips them silently."""
        return None
