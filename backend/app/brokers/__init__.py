from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.brokers.fake import FakeBrokerAdapter
from app.models.broker_account import BrokerAccount, BrokerName


def adapter_for(broker_account: BrokerAccount, credentials: dict) -> BrokerAdapter:
    """Construct an adapter for the broker_account using its decrypted credentials."""
    if broker_account.broker == BrokerName.ALPACA:
        return AlpacaAdapter(credentials)
    if broker_account.broker == BrokerName.FAKE:
        # Test-only — see app/brokers/fake.py. The credentials dict is
        # ignored; we keep the same call signature so copy_engine doesn't
        # have to branch on broker type. The seed script stores an empty
        # encrypted dict so the decrypt path still succeeds.
        return FakeBrokerAdapter(credentials)
    raise ValueError(f"no adapter for {broker_account.broker}")


__all__ = [
    "AlpacaAdapter",
    "BrokerAdapter",
    "BrokerOrderRequest",
    "BrokerOrderResult",
    "BrokerPosition",
    "ConnectionInfo",
    "FakeBrokerAdapter",
    "adapter_for",
    "build_occ_symbol",
]
