"""SnapTrade implementation of the BrokerAdapter interface.

Bound to a specific (app_user_id, user_secret, snaptrade_account_id) — caller
constructs one per place_order operation. Options are NOT yet routed through
SnapTrade in this build (their options coverage varies by broker; revisit
once we know which underlying brokers a given subscriber has linked).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    ConnectionInfo,
)
from app.models.order import InstrumentType, OrderSide, OrderStatus, OrderType
from app.services import snaptrade as st

_SIDE = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}

# SnapTrade order status strings → our enum.
_STATUS = {
    "PENDING": OrderStatus.SUBMITTED,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "EXECUTED": OrderStatus.FILLED,
    "FILLED": OrderStatus.FILLED,
    "PARTIAL": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}


def _map_status(s: str | None) -> OrderStatus:
    if not s:
        return OrderStatus.SUBMITTED
    return _STATUS.get(s.upper(), OrderStatus.SUBMITTED)


class SnapTradeBrokerAdapter(BrokerAdapter):
    name = "snaptrade"

    def __init__(
        self,
        *,
        app_user_id: uuid.UUID,
        user_secret: str,
        snaptrade_account_id: str,
    ):
        # Intentionally do not call super().__init__ — we don't store credentials;
        # the SnapTrade userSecret is enough.
        self.app_user_id = app_user_id
        self.user_secret = user_secret
        self.snaptrade_account_id = snaptrade_account_id

    def verify_connection(self) -> ConnectionInfo:
        accounts = st.list_accounts(self.app_user_id, self.user_secret)
        for a in accounts:
            if str(a.get("id")) == self.snaptrade_account_id:
                meta = a.get("meta") or {}
                return ConnectionInfo(
                    broker_account_id=str(a.get("number") or self.snaptrade_account_id),
                    supports_fractional=bool(meta.get("supports_fractional_units", False)),
                    extra=a,
                )
        raise ValueError("snaptrade_account_not_found")

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if req.instrument_type == InstrumentType.OPTION:
            raise NotImplementedError(
                "options not yet wired through SnapTrade — verify per-broker support first"
            )
        side = _SIDE[req.side]
        qty = float(req.quantity)
        if req.order_type == OrderType.MARKET:
            resp = st.place_market_order(
                self.app_user_id,
                self.user_secret,
                self.snaptrade_account_id,
                symbol=req.symbol,
                side=side,
                quantity=qty,
            )
        elif req.order_type == OrderType.LIMIT:
            resp = st.place_limit_order(
                self.app_user_id,
                self.user_secret,
                self.snaptrade_account_id,
                symbol=req.symbol,
                side=side,
                quantity=qty,
                limit_price=float(req.limit_price),
            )
        else:
            raise NotImplementedError(f"order_type {req.order_type} not yet wired through SnapTrade")

        return _resp_to_result(resp)

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        raw = st.get_order(self.app_user_id, self.user_secret, self.snaptrade_account_id, broker_order_id)
        if raw is None:
            raise ValueError("order_not_found")
        return _resp_to_result(raw)


def _resp_to_result(raw: dict[str, Any]) -> BrokerOrderResult:
    bid = str(raw.get("brokerage_order_id") or raw.get("id") or "")
    filled_qty = raw.get("filled_units") or raw.get("filled_quantity") or 0
    avg = raw.get("execution_price") or raw.get("filled_avg_price")
    return BrokerOrderResult(
        broker_order_id=bid,
        status=_map_status(raw.get("status")),
        submitted_at=datetime.now(timezone.utc),
        filled_quantity=Decimal(str(filled_qty)),
        filled_avg_price=Decimal(str(avg)) if avg else None,
        reject_reason=raw.get("reject_reason") or None,
    )
