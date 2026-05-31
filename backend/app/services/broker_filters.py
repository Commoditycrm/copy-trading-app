"""Listener-gating helpers.

The three per-account flags surfaced as the Brokers-page checkboxes
(``auto_pull_orders`` + ``bring_open_orders`` + ``bring_filled_orders``)
govern what each broker listener actually persists + fans out. We keep
the filter logic in one spot so all four listeners (SnapTrade / Alpaca /
Webull / IBKR) behave identically.

Decision table
--------------
auto_pull_orders=False
    Listener short-circuits at the top of the poll loop. No broker
    fetch, no persistence, no fanout. The poll task stays alive (so the
    flag can be flipped back on at runtime), it just sleeps and
    re-checks.

auto_pull_orders=True
    bring_open_orders   bring_filled_orders   → effect
    ─────────────────   ────────────────────    ─────────────────
    True                True                  → mirror everything (the historic default)
    True                False                 → mirror everything EXCEPT FILLED orders
    False               True                  → mirror ONLY FILLED orders (post-fill copy)
    False               False                 → mirror nothing (functionally same as auto_pull_orders=False)

"Filled" here means ``OrderStatus.FILLED`` specifically. ``PARTIALLY_FILLED``
counts as "open" because the rest of the order is still working — the
user's mental model is "filled = done."
"""
from __future__ import annotations

from app.models.broker_account import BrokerAccount
from app.models.order import OrderStatus

_FILLED = {OrderStatus.FILLED}


def auto_pull_enabled(acct: BrokerAccount | None) -> bool:
    """Master switch. False means the listener should skip the poll
    entirely — no broker call, no persistence. Returns False for an
    unknown/None account (defensive)."""
    return bool(acct and acct.auto_pull_orders)


def should_persist_order(acct: BrokerAccount | None, status: OrderStatus) -> bool:
    """Per-order filter checked inside ``_persist_and_fanout``. Returns
    True if this order's status passes both the master switch and the
    appropriate Bring-open / Bring-filled checkbox."""
    if acct is None or not acct.auto_pull_orders:
        return False
    if status in _FILLED:
        return bool(acct.bring_filled_orders)
    return bool(acct.bring_open_orders)
