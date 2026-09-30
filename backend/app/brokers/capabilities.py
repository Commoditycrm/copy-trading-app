"""Per-broker P&L capabilities, keyed by BrokerName.

The single source of truth for what each broker's API can authoritatively tell
us. Core P&L logic (pnl.py, pnl_snapshot.py) reads these through
``capabilities_for`` instead of switching on broker names, and each concrete
adapter exposes the same object as ``adapter.capabilities`` (wired in
app.brokers.__init__). Declaring a new broker's capabilities here is all it
takes to plug it into the P&L engine correctly.

Guiding rule: only set a flag True when the broker's API actually backs it.
Everything unstated stays False, so a broker we haven't verified is treated as
exposing nothing — we never present a confident number we can't source.
"""
from __future__ import annotations

from app.brokers.base import BrokerCapabilities
from app.models.broker_account import BrokerName

_CAPS: dict[BrokerName, BrokerCapabilities] = {
    # Alpaca: complete /account/activities feed (fills + OPEXP), a marked
    # portfolio-history series, and a live day-P&L snapshot. Everything.
    BrokerName.ALPACA: BrokerCapabilities(
        authoritative_fill_history=True,
        historical_daily_pnl=True,
        live_daily_pnl=True,
        authoritative_open_pnl=True,
        portfolio_history=True,
    ),
    # SnapTrade: complete get_account_activities feed (so realized/expiry is
    # sourceable) + current balances/positions. No marked history series.
    BrokerName.SNAPTRADE: BrokerCapabilities(
        authoritative_fill_history=True,
        live_daily_pnl=True,
        authoritative_open_pnl=True,
    ),
    # Webull DIRECT: only point-in-time reads — today's Day's P&L
    # (total_day_profit_loss) and current positions. NO activity/fill feed and
    # NO history, so we must NOT FIFO-infer realized or synthetic expiries here.
    BrokerName.WEBULL: BrokerCapabilities(
        live_daily_pnl=True,
        authoritative_open_pnl=True,
    ),
    # IBKR: not verified for this app; declare nothing until checked.
    BrokerName.IBKR: BrokerCapabilities(),
    # Fake: test broker — its "history" is whatever a test sets, so complete
    # by construction.
    BrokerName.FAKE: BrokerCapabilities(
        authoritative_fill_history=True,
        live_daily_pnl=True,
        authoritative_open_pnl=True,
    ),
}


def capabilities_for(broker: BrokerName | None) -> BrokerCapabilities:
    """Capabilities for a broker. ``None`` (a disconnected broker, whose history
    is by definition incomplete) and any unmapped broker get the all-False
    default — the safe side."""
    if broker is None:
        return BrokerCapabilities()
    return _CAPS.get(broker, BrokerCapabilities())
