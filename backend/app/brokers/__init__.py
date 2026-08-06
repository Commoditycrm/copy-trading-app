from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.brokers.fake import FakeBrokerAdapter
from app.brokers.ibkr import IBKRAdapter
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.broker_account import BrokerAccount, BrokerName


def adapter_for(broker_account: BrokerAccount, credentials: dict) -> BrokerAdapter:
    """Construct an adapter for the broker_account using its decrypted credentials.

    Note: ``BrokerName.WEBULL`` routes to the direct-Webull adapter ONLY when
    ``settings.webull_direct_enabled`` is on (the real-time gRPC trade-signal
    integration). With the flag off — the default — WEBULL accounts stay inert
    and this raises, exactly as before; users connect Webull via SnapTrade
    (``BrokerName.SNAPTRADE``). The SDK is imported lazily so it's only needed
    where direct Webull is actually used.
    """
    if broker_account.broker == BrokerName.ALPACA:
        return AlpacaAdapter(credentials)
    if broker_account.broker == BrokerName.SNAPTRADE:
        return SnapTradeAdapter(credentials)
    if broker_account.broker == BrokerName.IBKR:
        return IBKRAdapter(credentials)
    if broker_account.broker == BrokerName.WEBULL:
        from app.config import get_settings  # noqa: PLC0415
        if get_settings().webull_direct_enabled:
            from app.brokers.webull import WebullAdapter  # noqa: PLC0415
            return WebullAdapter(credentials)
        raise ValueError(
            "webull_direct_enabled is off — direct Webull accounts are inert"
        )
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
    "IBKRAdapter",
    "SnapTradeAdapter",
    "adapter_for",
    "build_occ_symbol",
]
