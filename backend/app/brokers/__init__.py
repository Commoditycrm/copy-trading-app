from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.broker_account import BrokerAccount, BrokerName


def adapter_for(broker_account: BrokerAccount, credentials: dict) -> BrokerAdapter:
    """Construct an adapter for the broker_account using its decrypted credentials."""
    if broker_account.broker == BrokerName.ALPACA:
        return AlpacaAdapter(credentials)
    raise ValueError(f"no adapter for {broker_account.broker}")


__all__ = [
    "AlpacaAdapter",
    "BrokerAdapter",
    "BrokerOrderRequest",
    "BrokerOrderResult",
    "BrokerPosition",
    "ConnectionInfo",
    "adapter_for",
    "build_occ_symbol",
]
