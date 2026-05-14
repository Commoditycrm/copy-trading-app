"""Alpaca direct integration.

Credentials shape (Fernet-encrypted in broker_accounts.encrypted_credentials):
    {"api_key": "...", "api_secret": "...", "paper": true}

Handles both stocks AND options. Options use OCC symbols which we build from
(expiry, strike, right). Alpaca's order endpoint accepts the same Market/Limit
request types for both — only the symbol shape distinguishes them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import OrderStatus as AlpacaStatus
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    ConnectionInfo,
)
from app.models.order import InstrumentType, OrderSide, OrderStatus, OrderType

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
}


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
        common = {
            "symbol": sym,
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": req.client_order_id,
        }
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
        return BrokerOrderResult(
            broker_order_id=str(resp.id),
            status=_STATUS_IN.get(resp.status, OrderStatus.SUBMITTED),
            submitted_at=resp.submitted_at or datetime.now(timezone.utc),
            filled_quantity=Decimal(str(resp.filled_qty or 0)),
            filled_avg_price=Decimal(str(resp.filled_avg_price)) if resp.filled_avg_price else None,
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

    def cancel_order(self, broker_order_id: str) -> None:
        self._c().cancel_order_by_id(broker_order_id)

    # ── reads — used by sync, balance refresh, options chain ──────────────

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
        """Activities = fills, dividends, etc. Caller filters by type."""
        return self._c().get_account_activities()

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
