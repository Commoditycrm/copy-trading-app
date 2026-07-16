"""Server-side equivalents of the Trades page's client-side filters.

The /trades page loads a window of orders and then filters them IN THE
BROWSER — status tab, symbol search, and a bracket-leg noise filter. An
export built from the DB has to reproduce those exactly, or the file won't
match what the user saw when they clicked Export.

MUST STAY IN SYNC with frontend/app/(app)/trades/page.tsx:
  - OPEN_STATUSES / WORKING_STATUSES
  - matchesStatusTab()
  - the `baseOrders` bracket-leg exclusion
Change one, change the other.
"""
from __future__ import annotations

from sqlalchemy import Select, or_

from app.models.order import Order, OrderStatus

# Mirrors OPEN_STATUSES / WORKING_STATUSES in trades/page.tsx.
OPEN_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)
WORKING_STATUSES = (*OPEN_STATUSES, OrderStatus.RETRY_PENDING)

# Mirrors matchesStatusTab(). "all" is absent on purpose — it means no filter.
STATUS_TABS = ("all", "working", "filled", "cancelled", "rejected")
_TAB_STATUSES = {
    "working": WORKING_STATUSES,
    "filled": (OrderStatus.FILLED,),
    # Expired never filled and isn't working, so the UI groups it with
    # cancelled ("didn't fill, not rejected").
    "cancelled": (OrderStatus.CANCELED, OrderStatus.EXPIRED),
    "rejected": (OrderStatus.REJECTED,),
}


def apply_status_tab(q: Select, tab: str) -> Select:
    """Filter to one of the Trades page's status tabs."""
    statuses = _TAB_STATUSES.get(tab)
    if statuses is None:          # "all" or unknown -> no filter
        return q
    return q.where(Order.status.in_(statuses))


def apply_symbol_search(q: Select, search: str | None) -> Select:
    """Case-insensitive symbol substring — same as the page's search box
    (`o.symbol.toUpperCase().includes(q)`)."""
    term = (search or "").strip()
    if not term:
        return q
    return q.where(Order.symbol.ilike(f"%{term}%"))


def exclude_dead_bracket_legs(q: Select) -> Select:
    """Drop bracket-exit legs that never happened.

    The UI hides these (`baseOrders`): when a bracket entry closes, its unused
    TP/SL leg lands as canceled/rejected/expired and is pure noise. Without
    this an export shows rows the user never saw on screen and can't explain.
    """
    return q.where(
        or_(
            Order.bracket_parent_id.is_(None),
            Order.status.notin_(
                (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)
            ),
        )
    )
