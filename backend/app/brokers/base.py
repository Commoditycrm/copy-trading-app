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


@dataclass(frozen=True)
class BrokerOrderResult:
    broker_order_id: str
    status: OrderStatus
    submitted_at: datetime
    filled_quantity: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    reject_reason: str | None = None


class BrokerAdapter(ABC):
    """One instance per BrokerAccount. Hold decrypted credentials in-memory only."""

    name: str

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

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError
