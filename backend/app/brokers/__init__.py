from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.brokers.base import (
    BrokerAdapter,
    BrokerCapabilities,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.brokers.capabilities import capabilities_for
from app.brokers.fake import FakeBrokerAdapter
from app.brokers.ibkr import IBKRAdapter
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.broker_account import BrokerAccount, BrokerName

# Expose each broker's capabilities on its adapter class (single source is
# app.brokers.capabilities). WebullAdapter is imported lazily inside adapter_for
# to avoid its SDK at import time, so it reads capabilities_for(WEBULL) itself.
AlpacaAdapter.capabilities = capabilities_for(BrokerName.ALPACA)
SnapTradeAdapter.capabilities = capabilities_for(BrokerName.SNAPTRADE)
IBKRAdapter.capabilities = capabilities_for(BrokerName.IBKR)
FakeBrokerAdapter.capabilities = capabilities_for(BrokerName.FAKE)


# Which brokers can rest a take-profit LINKED to a stop on the SAME held
# contracts, one filling cancelling the other. Mirrors
# ``BrokerAdapter.supports_exit_pair``, but answerable from a BrokerAccount
# alone — callers deciding whether that exit mode is RUNNING have no adapter,
# and building one needs decrypted credentials (and Webull's SDK). Kept in step
# with the adapter classes by test_exit_pair_capability.py.
_EXIT_PAIR_BROKERS = frozenset({BrokerName.WEBULL})


def broker_supports_exit_pair(broker: BrokerName | None) -> bool:
    """Can this broker rest a linked take-profit/stop pair on a held position?"""
    return broker in _EXIT_PAIR_BROKERS


def adapter_for(broker_account: BrokerAccount, credentials: dict) -> BrokerAdapter:
    """Construct an adapter for the broker_account using its decrypted credentials.

    Note: ``BrokerName.WEBULL`` routes to the direct-Webull adapter ONLY when
    ``settings.webull_direct_enabled`` is on. That adapter now serves BOTH roles
    — a trader's real-time fill signal AND a subscriber's mirror execution — so
    with the flag on it is on the live order path, not just a read.

    With the flag off (the default) WEBULL accounts stay inert and this raises;
    Webull is then reachable only through the SnapTrade aggregator, which lands
    as ``BrokerName.SNAPTRADE`` rows. The SDK is imported lazily so it's only
    needed where direct Webull is actually used.
    """
    if broker_account.broker == BrokerName.ALPACA:
        from app.services import paper_autotrade  # noqa: PLC0415

        adapter = AlpacaAdapter(credentials)
        if paper_autotrade.guards(getattr(broker_account, "id", None)):
            paper_autotrade.attach(adapter, broker_account.id, credentials)
        return adapter
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
    "BrokerCapabilities",
    "capabilities_for",
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
