"""A position's fills in order — the Positions page's "position summary".

    Buy  10 @ 5.20   Rem.Qty 10
    Sell  5 @ 5.79   Rem.Qty  5
    Sell  5 @ 6.10   Rem.Qty  0

Read from our own order rows for one contract on one broker account: every
order that filled anything, in fill-time order, with the running remaining
quantity. A contract can be opened and closed more than once, so the history is
cut into holdings at each point the remaining quantity returns to zero, and one
holding is returned: the open one, or the one a given closing order belongs to.

Only what went through Kopyya is here: a fill made in the broker's own app is
not an order of ours, so the running quantity can disagree with the broker's.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import OptionRight, Order, OrderSide


def _when(o: Order):
    return o.broker_filled_at or o.closed_at or o.submitted_at or o.created_at


def _s(v) -> str | None:
    return None if v is None else f"{Decimal(str(v)).normalize():f}"


def holding(db: Session, user_id, broker_account_id, symbol: str, *, strike: Decimal | None = None,
            right: str | None = None, expiry: date | None = None,
            through_order_id: uuid.UUID | None = None) -> list[dict]:
    """The fills of one holding of this contract, oldest first (see module doc)."""
    q = select(Order).where(
        Order.user_id == user_id,
        Order.broker_account_id == broker_account_id,
        Order.symbol == symbol.upper(),
        Order.filled_quantity > 0,
    )
    right_enum = OptionRight(right) if right else None
    for col, val in ((Order.option_strike, strike), (Order.option_right, right_enum), (Order.option_expiry, expiry)):
        q = q.where(col.is_(None) if val is None else col == val)
    orders = sorted(db.execute(q).scalars(), key=lambda o: (_when(o) is None, _when(o)))

    holdings: list[list[dict]] = [[]]
    rem = Decimal(0)
    for o in orders:
        qty = Decimal(str(o.filled_quantity))
        rem += qty if o.side == OrderSide.BUY else -qty
        when = _when(o)
        holdings[-1].append({
            "order_id": str(o.id),
            "side": "buy" if o.side == OrderSide.BUY else "sell",
            "quantity": _s(qty),
            "price": _s(o.filled_avg_price),
            "at": when.isoformat() if when else None,
            "remaining": _s(abs(rem)),
        })
        if rem == 0:
            holdings.append([])

    if through_order_id is not None:
        wanted = str(through_order_id)
        return next((h for h in holdings if any(f["order_id"] == wanted for f in h)), [])
    return holdings[-1]


__all__ = ["holding"]
