"""A position's history — the Positions page's "position summary".

``timeline()`` is what the page shows: every order of the holding (what was
asked for — qty, market / limit price — and what filled), labelled Entry,
Average, Add, T1, T2 …, Stopped out; merged in time order with the stop's own
history (set, moved, trailing raised, removed — services/position_events).
``holding()`` underneath is the fills alone:

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

from datetime import datetime, timedelta, timezone

from app.models.order import OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.position_event import PositionEvent


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


_STOP_TYPES = (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP)
_UNFILLED_SHOWN = (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
                   OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED)


def _utc(t):
    if t is not None and t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


def _px(v: Decimal | None) -> str | None:
    """A price for display: at least 2 decimals, at most 4 (2 -> "2.00",
    2.0333.. -> "2.0333")."""
    if v is None:
        return None
    q = f"{Decimal(v).quantize(Decimal('0.0001')):f}".rstrip("0")
    whole, _, frac = q.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _requested(o: Order) -> str:
    qty = _s(o.quantity)
    if o.order_type == OrderType.MARKET:
        return f"{qty} @ market"
    if o.order_type == OrderType.LIMIT:
        return f"{qty} @ {_s(o.limit_price)} limit"
    if o.order_type == OrderType.TRAILING_STOP:
        trail = f"{_s(o.trail_percent)}%" if getattr(o, "trail_percent", None) else f"${_s(getattr(o, 'trail_price', None))}"
        return f"{qty} trailing {trail}"
    return f"{qty} stop @ {_s(o.stop_price)}" + (f" limit {_s(o.limit_price)}" if o.limit_price else "")


_EVENT_LABELS = {
    "stop_set": "Stop set",
    "stop_moved": "Stop moved",
    "stop_removed": "Stop removed",
    "trailing_stop_set": "Trailing stop set",
    "trailing_stop_raised": "Trailing stop raised",
    "trailing_exit_armed": "Trailing exit armed",
    "trailing_exit_cleared": "Trailing exit cleared",
}


def _event_detail(e: PositionEvent) -> str:
    trail = f"{_s(e.trail_pct)}% below high {_s(e.peak)}" if e.trail_pct is not None and e.peak is not None else None
    if e.kind == "stop_removed":
        return f"was {_s(e.old_price)}"
    if e.kind == "trailing_exit_armed":
        give = f"${_s(e.trail_amount)}" if e.trail_amount is not None else f"{_s(e.trail_pct)}%"
        return f"{_s(e.quantity)} rides a {give} give-back from {_s(e.peak)}" + (f" (exits at {_s(e.price)})" if e.price else "")
    if e.kind == "trailing_exit_cleared":
        return f"{_s(e.quantity)} no longer trailing"
    moved = f"{_s(e.old_price)} → {_s(e.price)}" if e.old_price is not None else f"@ {_s(e.price)}"
    return moved + (f" · {trail}" if trail else "")


def timeline(db: Session, user_id, broker_account_id, symbol: str, *, strike: Decimal | None = None,
             right: str | None = None, expiry: date | None = None,
             through_order_id: uuid.UUID | None = None) -> list[dict]:
    """Everything that happened to one holding, oldest first (see module doc)."""
    fills = holding(db, user_id, broker_account_id, symbol, strike=strike, right=right,
                    expiry=expiry, through_order_id=through_order_id)
    if not fills:
        return []
    remaining = {f["order_id"]: f["remaining"] for f in fills}

    right_enum = OptionRight(right) if right else None
    q = select(Order).where(
        Order.user_id == user_id,
        Order.broker_account_id == broker_account_id,
        Order.symbol == symbol.upper(),
    )
    for col, val in ((Order.option_strike, strike), (Order.option_right, right_enum), (Order.option_expiry, expiry)):
        q = q.where(col.is_(None) if val is None else col == val)
    orders = list(db.execute(q).scalars())
    in_holding = [o for o in orders if str(o.id) in remaining]
    start = min(_utc(o.created_at) for o in in_holding if o.created_at) if any(o.created_at for o in in_holding) else None
    closed = fills[-1]["remaining"] == "0"
    end = None                            # open: up to now
    if closed:
        last = max((_utc(_when(o)) for o in in_holding if _when(o)), default=None)
        end = last + timedelta(minutes=2) if last else None

    def inside(t) -> bool:
        t = _utc(t)
        return t is not None and (start is None or t >= start) and (end is None or t <= end)

    items: list[tuple] = []
    buys = sells = 0
    avg = Decimal(0)
    held = Decimal(0)
    for o in sorted(orders, key=lambda o: (_utc(_when(o)) or datetime.min.replace(tzinfo=timezone.utc))):
        filled = Decimal(str(o.filled_quantity or 0))
        mine = str(o.id) in remaining
        if not mine:
            if filled > 0 or o.status not in _UNFILLED_SHOWN or o.order_type in _STOP_TYPES:
                continue                  # another holding's fill, a replaced order, a resting stop
            if not inside(o.created_at):
                continue
        buy = o.side == OrderSide.BUY
        price = Decimal(str(o.filled_avg_price)) if o.filled_avg_price is not None else None
        if mine and buy:
            buys += 1
            label = "Entry" if buys == 1 else ("Average" if price is not None and avg and price < avg else "Add")
            if price is not None:
                avg = ((avg * held) + price * filled) / (held + filled) if held + filled else price
            held += filled
        elif mine:
            if o.order_type in _STOP_TYPES:
                label = "Stopped out"
            else:
                sells += 1
                label = f"T{sells}"
            held -= filled
            if held <= 0:
                avg = Decimal(0)           # flat: nothing left to have a cost
        else:
            label = "Buy order" if buy else "Sell order"
        at = _when(o) if mine else o.created_at
        items.append((_utc(at), {
            "type": "order",
            "at": _utc(at).isoformat() if at else None,
            "label": label,
            "side": "buy" if buy else "sell",
            "requested": _requested(o),
            "filled": f"{_s(filled)} @ {_s(price)}" if mine else None,
            "status": o.status.value,
            "remaining": remaining.get(str(o.id)),
            # Average cost of what is still held after this fill. A sell
            # doesn't change it; flat has none.
            "avg_price": (_px(avg) if mine and held > 0 and avg else None),
            "detail": None,
        }))

    ev_q = select(PositionEvent).where(
        PositionEvent.user_id == user_id,
        PositionEvent.symbol == symbol.upper(),
    )
    for col, val in ((PositionEvent.option_strike, strike), (PositionEvent.option_right, right),
                     (PositionEvent.option_expiry, expiry)):
        ev_q = ev_q.where(col.is_(None) if val is None else col == val)
    try:
        events = list(db.execute(ev_q).scalars())
    except Exception:  # noqa: BLE001 — a database without the table yet
        events = []
    for e in events:
        if not inside(e.created_at):
            continue
        at = _utc(e.created_at)
        items.append((at, {
            "type": "event",
            "at": at.isoformat(),
            "label": _EVENT_LABELS.get(e.kind, e.kind),
            "side": None, "requested": None, "filled": None, "status": None, "remaining": None,
            "avg_price": None,
            "detail": _event_detail(e),
        }))

    items.sort(key=lambda it: (it[0] or datetime.min.replace(tzinfo=timezone.utc)))
    return [i for _, i in items]


__all__ = ["holding", "timeline"]
