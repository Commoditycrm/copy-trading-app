"""Alpaca direct integration.

Credentials shape (Fernet-encrypted in broker_accounts.encrypted_credentials):
    {"api_key": "...", "api_secret": "...", "paper": true}

Handles both stocks AND options. Options use OCC symbols which we build from
(expiry, strike, right). Alpaca's order endpoint accepts the same Market/Limit
request types for both — only the symbol shape distinguishes them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import OrderStatus as AlpacaStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderLeg,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType

# Map Alpaca → our enums
_SIDE_OUT = {OrderSide.BUY: AlpacaSide.BUY, OrderSide.SELL: AlpacaSide.SELL}
_STATUS_IN = {
    AlpacaStatus.NEW: OrderStatus.SUBMITTED,
    AlpacaStatus.ACCEPTED: OrderStatus.ACCEPTED,
    AlpacaStatus.PENDING_NEW: OrderStatus.SUBMITTED,
    AlpacaStatus.ACCEPTED_FOR_BIDDING: OrderStatus.ACCEPTED,
    AlpacaStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaStatus.FILLED: OrderStatus.FILLED,
    AlpacaStatus.DONE_FOR_DAY: OrderStatus.EXPIRED,
    AlpacaStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaStatus.REPLACED: OrderStatus.SUBMITTED,
    AlpacaStatus.PENDING_CANCEL: OrderStatus.SUBMITTED,
    AlpacaStatus.PENDING_REPLACE: OrderStatus.SUBMITTED,
    AlpacaStatus.REJECTED: OrderStatus.REJECTED,
    AlpacaStatus.SUSPENDED: OrderStatus.SUBMITTED,
    AlpacaStatus.CALCULATED: OrderStatus.FILLED,
    # A bracket's TP/SL legs sit HELD until the entry fills — treat as a live
    # (accepted) working order so they surface as the subscriber's SL/TP rows.
    AlpacaStatus.HELD: OrderStatus.ACCEPTED,
}


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


_OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


def _looks_like_occ(s: str) -> bool:
    return bool(_OCC_RE.match(s))


def _parse_occ(s: str) -> tuple[str, date, Decimal, OptionRight] | None:
    """OCC 21-char option symbol → (root, expiry, strike, right). Returns
    None if it doesn't match the format."""
    m = _OCC_RE.match(s)
    if not m:
        return None
    root, yymmdd, cp, strike_str = m.groups()
    try:
        yy = int(yymmdd[:2])
        # OCC uses 2-digit years; convention is 20XX (good for any near-future
        # expiry — Alpaca options listings rarely go past 2050 anyway).
        year = 2000 + yy
        expiry = date(year, int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    strike = Decimal(strike_str) / Decimal(1000)
    right = OptionRight.CALL if cp == "C" else OptionRight.PUT
    return root, expiry, strike, right


def build_occ_symbol(symbol: str, expiry: date, strike: Decimal, right: str) -> str:
    """OCC 21-char option symbol. Example: AAPL 2025-07-19 $200 CALL → AAPL250719C00200000.

    Note: Alpaca's order API accepts the no-space form (concatenated 21 chars when
    the root is ≥6 chars, padded otherwise). We pad the root to 6 with no spaces
    inside — that matches what their order endpoint wants. Their option-contracts
    listing returns the same form."""
    root = symbol.upper()
    yy = expiry.strftime("%y%m%d")
    cp = "C" if right.lower() == "call" else "P"
    strike_int = int(strike * Decimal(1000))
    return f"{root}{yy}{cp}{strike_int:08d}"


@dataclass
class AlpacaCredentials:
    api_key: str
    api_secret: str
    paper: bool = True


log = logging.getLogger(__name__)


class AlpacaAdapter(BrokerAdapter):
    name = "alpaca"

    def __init__(self, credentials: dict[str, Any]):
        self.credentials = credentials
        self._client: TradingClient | None = None

    def _c(self) -> TradingClient:
        if self._client is None:
            self._client = TradingClient(
                api_key=self.credentials["api_key"],
                secret_key=self.credentials["api_secret"],
                paper=bool(self.credentials.get("paper", True)),
            )
        return self._client

    # ── connection ────────────────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        a = self._c().get_account()
        return ConnectionInfo(
            broker_account_id=str(a.account_number),
            supports_fractional=True,
            extra={
                "status": str(a.status),
                "currency": a.currency,
                "cash": str(a.cash),
                "buying_power": str(a.buying_power),
                "equity": str(a.equity),
                "options_approved_level": getattr(a, "options_approved_level", None),
                "options_trading_level": getattr(a, "options_trading_level", None),
                "options_buying_power": getattr(a, "options_buying_power", None),
            },
        )

    # ── orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        # Build the symbol: OCC for options, plain ticker for stocks.
        if req.instrument_type == InstrumentType.OPTION:
            if not (req.option_expiry and req.option_strike and req.option_right):
                raise ValueError("option order missing expiry/strike/right")
            sym = build_occ_symbol(
                req.symbol, req.option_expiry, req.option_strike, req.option_right.value,
            )
            qty = int(req.quantity)   # options trade in whole contracts
        else:
            sym = req.symbol.upper()
            qty = float(req.quantity)

        side = _SIDE_OUT[req.side]
        # Attached exits (take_profit and/or stop_loss) require a "complex" order
        # class + GTC TIF. Alpaca has two flavours:
        #   * BOTH legs → OrderClass.BRACKET (OTOCO).
        #   * ONE leg   → OrderClass.OTO (One-Triggers-Other) — BRACKET would be
        #     rejected because it demands both legs.
        # Either way the exit(s) arm only once the entry fills.
        has_tp = req.take_profit_price is not None
        has_sl = req.stop_loss_price is not None
        is_advanced = (has_tp or has_sl) and req.order_type in (OrderType.MARKET, OrderType.LIMIT)
        common = {
            "symbol": sym,
            "qty": qty,
            "side": side,
            # Complex exits may not fire same-day, so they require GTC.
            # Plain orders keep DAY (cancel at session close).
            "time_in_force": TimeInForce.GTC if is_advanced else TimeInForce.DAY,
            "client_order_id": req.client_order_id,
        }
        if is_advanced:
            common["order_class"] = (
                AlpacaOrderClass.BRACKET if (has_tp and has_sl) else AlpacaOrderClass.OTO
            )
            if has_tp:
                common["take_profit"] = TakeProfitRequest(
                    limit_price=float(req.take_profit_price)
                )
            if has_sl:
                common["stop_loss"] = StopLossRequest(
                    stop_price=float(req.stop_loss_price)
                )
        if req.order_type == OrderType.MARKET:
            order_req = MarketOrderRequest(**common)
        elif req.order_type == OrderType.LIMIT:
            order_req = LimitOrderRequest(**common, limit_price=float(req.limit_price))
        elif req.order_type == OrderType.STOP:
            order_req = StopOrderRequest(**common, stop_price=float(req.stop_price))
        elif req.order_type == OrderType.STOP_LIMIT:
            order_req = StopLimitOrderRequest(
                **common,
                limit_price=float(req.limit_price),
                stop_price=float(req.stop_price),
            )
        else:
            raise ValueError(f"unsupported order_type {req.order_type}")

        resp = self._c().submit_order(order_req)
        # A bracket submit returns its child legs (TP/SL) under `resp.legs`, each
        # a held order with its own id. Surface them so the copy engine can show
        # them as the subscriber's SL/TP rows (Alpaca activates them on fill).
        legs: list[BrokerOrderLeg] = []
        for leg in (getattr(resp, "legs", None) or []):
            try:
                lt = str(getattr(leg.order_type, "value", leg.order_type)).lower()
                legs.append(BrokerOrderLeg(
                    broker_order_id=str(leg.id),
                    side=OrderSide.SELL if str(getattr(leg.side, "value", leg.side)).lower() == "sell" else OrderSide.BUY,
                    order_type={
                        "market": OrderType.MARKET, "limit": OrderType.LIMIT,
                        "stop": OrderType.STOP, "stop_limit": OrderType.STOP_LIMIT,
                    }.get(lt, OrderType.LIMIT),
                    status=_STATUS_IN.get(leg.status, OrderStatus.ACCEPTED),
                    limit_price=Decimal(str(leg.limit_price)) if getattr(leg, "limit_price", None) else None,
                    stop_price=Decimal(str(leg.stop_price)) if getattr(leg, "stop_price", None) else None,
                ))
            except Exception:  # noqa: BLE001
                log.exception("alpaca: failed to parse bracket leg %s", getattr(leg, "id", "?"))
        return BrokerOrderResult(
            broker_order_id=str(resp.id),
            status=_STATUS_IN.get(resp.status, OrderStatus.SUBMITTED),
            submitted_at=resp.submitted_at or datetime.now(timezone.utc),
            filled_quantity=Decimal(str(resp.filled_qty or 0)),
            filled_avg_price=Decimal(str(resp.filled_avg_price)) if resp.filled_avg_price else None,
            bracket_legs=tuple(legs),
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        resp = self._c().get_order_by_id(broker_order_id)
        return BrokerOrderResult(
            broker_order_id=str(resp.id),
            status=_STATUS_IN.get(resp.status, OrderStatus.SUBMITTED),
            submitted_at=resp.submitted_at or datetime.now(timezone.utc),
            filled_quantity=Decimal(str(resp.filled_qty or 0)),
            filled_avg_price=Decimal(str(resp.filled_avg_price)) if resp.filled_avg_price else None,
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        """True — Alpaca raises if the order isn't cancellable, so reaching the
        return means we really did cancel a working order. See base.cancel_order
        for why the distinction matters."""
        self._c().cancel_order_by_id(broker_order_id)
        return True

    # ── positions ─────────────────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        """Return currently held positions. Alpaca returns one row per symbol
        per account (net qty). asset_class distinguishes stock vs option."""
        raw = self._c().get_all_positions() or []
        out: list[BrokerPosition] = []
        for p in raw:
            sym = str(p.symbol)
            asset_class = str(getattr(p, "asset_class", "") or "").lower()
            is_option = "option" in asset_class or _looks_like_occ(sym)
            instrument = InstrumentType.OPTION if is_option else InstrumentType.STOCK

            # alpaca-py returns qty as a signed string; "side" is "long"/"short"
            # but the sign on qty is the canonical signal.
            qty = _dec_or_none(getattr(p, "qty", None)) or Decimal(0)

            expiry = strike = right = None
            display_symbol = sym
            if is_option:
                parsed = _parse_occ(sym)
                if parsed:
                    display_symbol, expiry, strike, right = parsed

            out.append(BrokerPosition(
                broker_symbol=sym,
                symbol=display_symbol,
                instrument_type=instrument,
                quantity=qty,
                avg_entry_price=_dec_or_none(getattr(p, "avg_entry_price", None)),
                current_price=_dec_or_none(getattr(p, "current_price", None)),
                market_value=_dec_or_none(getattr(p, "market_value", None)),
                unrealized_pnl=_dec_or_none(getattr(p, "unrealized_pl", None)),
                cost_basis=_dec_or_none(getattr(p, "cost_basis", None)),
                option_expiry=expiry,
                option_strike=strike,
                option_right=right,
            ))
        return out

    # ── reads — used by sync, balance refresh, options chain ──────────────

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """Alpaca-direct: one ``GET /v2/account`` gives us live equity +
        last_equity (yesterday's close). Returns None on any failure so
        the poller skips this tick rather than killing the loop."""
        try:
            a = self._c().get_account()
            equity = Decimal(str(a.equity))
            last_equity = Decimal(str(a.last_equity))
        except Exception:  # noqa: BLE001
            log.warning("alpaca get_pnl_snapshot failed", exc_info=True)
            return None
        return {
            "todays_pl":             equity - last_equity,
            "equity":                equity,
            "beginning_day_balance": last_equity,
        }

    def get_balance_snapshot(self) -> dict[str, Any]:
        """Returns normalized balance numbers for the broker_accounts row."""
        a = self._c().get_account()
        def _dec(v: Any) -> Decimal | None:
            try:
                return Decimal(str(v)) if v is not None else None
            except Exception:  # noqa: BLE001
                return None
        return {
            "cash": _dec(a.cash),
            "buying_power": _dec(a.buying_power),
            "total_equity": _dec(a.equity),
            "currency": a.currency,
        }

    def list_recent_activities(self) -> list[Any]:
        """Activities = fills, dividends, etc. Caller filters by type.

        The alpaca-py SDK doesn't expose a typed helper for /v2/account/activities
        in current versions, so we hit the raw endpoint and return the list of
        dicts as-is. Downstream code uses `_attr` for tolerant dict-or-object
        access so both shapes work.
        """
        try:
            resp = self._c().get("/account/activities")
        except Exception:  # noqa: BLE001
            return []
        # Some SDK versions wrap the response in {"data": [...]}; most return a
        # bare list. Normalise to list.
        if isinstance(resp, dict) and "data" in resp:
            return list(resp["data"]) if isinstance(resp["data"], list) else []
        return list(resp) if isinstance(resp, list) else []

    def list_option_contracts(
        self,
        underlying: str,
        expiry: date | None = None,
        expiry_gte: date | None = None,
        expiry_lte: date | None = None,
        limit: int = 200,
    ) -> list[Any]:
        """List option contracts for an underlying. Used by the chain UI to
        populate expiry / strike dropdowns."""
        params: dict[str, Any] = {
            "underlying_symbols": [underlying.upper()],
            "limit": limit,
        }
        if expiry:        params["expiration_date"] = expiry
        if expiry_gte:    params["expiration_date_gte"] = expiry_gte
        if expiry_lte:    params["expiration_date_lte"] = expiry_lte
        resp = self._c().get_option_contracts(GetOptionContractsRequest(**params))
        # Response is a paginated object with .option_contracts
        return list(getattr(resp, "option_contracts", []) or [])

    def get_stock_latest_price(self, symbol: str) -> Decimal | None:
        """Latest known price for a stock symbol. Used by the trade panel's
        strike picker so it can default to the strike nearest the underlying
        (ATM) — picking the median of the chain is a poor approximation for
        skewed chains.

        Tries the latest QUOTE first (mid of bid/ask if both present, else
        whichever side is set); falls back to the latest TRADE if no quote
        is available (low-liquidity tickers between sessions, etc.).

        Feed selection: defaults to IEX, the only free feed that paper +
        free-tier API keys are entitled to. SIP returns 403 ``unauthorized``
        for those accounts, which previously surfaced as a silent None —
        the strike picker would then fall back to the chain median and
        the user would see AAPL default to ~$297 instead of ~$312.

        Returns None on any failure so the caller can fall back to its
        median-based pick rather than 500-ing the whole strikes request.
        """
        # Local imports — these data-API clients aren't needed by the
        # trading hot path, and lazy-importing keeps adapter construction
        # cheap for endpoints that don't touch quotes.
        from alpaca.data.enums import DataFeed  # noqa: PLC0415
        from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: PLC0415
        from alpaca.data.requests import (  # noqa: PLC0415
            StockLatestBarRequest,
            StockLatestQuoteRequest,
            StockLatestTradeRequest,
        )

        client = StockHistoricalDataClient(
            api_key=self.credentials["api_key"],
            secret_key=self.credentials["api_secret"],
        )
        sym = symbol.upper()

        try:
            quotes = client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=sym, feed=DataFeed.IEX)
            )
            q = quotes.get(sym) if isinstance(quotes, dict) else None
            bid = _dec_or_none(getattr(q, "bid_price", None)) if q else None
            ask = _dec_or_none(getattr(q, "ask_price", None)) if q else None
            if bid and ask and bid > 0 and ask > 0:
                px = (bid + ask) / Decimal(2)
                log.info("get_stock_latest_price(%s) → %s (quote mid)", sym, px)
                return px
            if ask and ask > 0:
                log.info("get_stock_latest_price(%s) → %s (ask only)", sym, ask)
                return ask
            if bid and bid > 0:
                log.info("get_stock_latest_price(%s) → %s (bid only)", sym, bid)
                return bid
        except Exception as exc:  # noqa: BLE001
            log.warning("get_stock_latest_price(%s): quote lookup failed: %s", sym, exc)

        try:
            trades = client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX)
            )
            t = trades.get(sym) if isinstance(trades, dict) else None
            px = _dec_or_none(getattr(t, "price", None)) if t else None
            if px and px > 0:
                log.info("get_stock_latest_price(%s) → %s (last trade)", sym, px)
                return px
        except Exception as exc:  # noqa: BLE001
            log.warning("get_stock_latest_price(%s): trade lookup failed: %s", sym, exc)

        # Bar fallback — bars publish for *every* symbol Alpaca knows
        # about, regardless of which exchange the stock trades on. AMZN,
        # GOOG, MSFT, etc. (NASDAQ-listed, light IEX volume) often have
        # NO IEX quote or trade outside RTH, which previously made the
        # picker fall back to the chain median and pick a wildly OTM
        # strike. The latest bar's close price is a reliable "last
        # known mid" and works for any ticker.
        try:
            bars = client.get_stock_latest_bar(
                StockLatestBarRequest(symbol_or_symbols=sym, feed=DataFeed.IEX)
            )
            b = bars.get(sym) if isinstance(bars, dict) else None
            close = _dec_or_none(getattr(b, "close", None)) if b else None
            if close and close > 0:
                log.info("get_stock_latest_price(%s) → %s (bar close)", sym, close)
                return close
        except Exception as exc:  # noqa: BLE001
            log.warning("get_stock_latest_price(%s): bar lookup failed: %s", sym, exc)

        log.warning("get_stock_latest_price(%s): no usable price returned", sym)
        return None

    def get_option_latest_quote(self, occ_symbol: str) -> tuple[Decimal | None, Decimal | None]:
        """Latest bid + ask for an OCC option symbol. Used by the trade
        panel to surface live pricing alongside the strike picker and to
        seed the Limit price field with the ask (the conventional buyer
        default).

        Returns (bid, ask) as Decimals. Either side may be None when the
        broker returns a zero / missing quote (illiquid contracts late
        in the trading day are the common case). On any API failure we
        return (None, None) so the caller can fall back to manual entry
        rather than 500-ing.

        Options data on Alpaca is OPRA-feed under the hood; paper + free
        keys are entitled to it, so no explicit feed param needed."""
        from alpaca.data.historical.option import OptionHistoricalDataClient  # noqa: PLC0415
        from alpaca.data.requests import OptionLatestQuoteRequest  # noqa: PLC0415

        client = OptionHistoricalDataClient(
            api_key=self.credentials["api_key"],
            secret_key=self.credentials["api_secret"],
        )
        try:
            quotes = client.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
            )
            q = quotes.get(occ_symbol) if isinstance(quotes, dict) else None
            bid = _dec_or_none(getattr(q, "bid_price", None)) if q else None
            ask = _dec_or_none(getattr(q, "ask_price", None)) if q else None
            # Treat 0.00 as "no quote" — Alpaca returns 0 for both sides
            # outside RTH on illiquid contracts.
            if bid is not None and bid <= 0:
                bid = None
            if ask is not None and ask <= 0:
                ask = None
            log.info(
                "get_option_latest_quote(%s) → bid=%s ask=%s", occ_symbol, bid, ask
            )
            return bid, ask
        except Exception as exc:  # noqa: BLE001
            log.warning("get_option_latest_quote(%s) failed: %s", occ_symbol, exc)
            return None, None
