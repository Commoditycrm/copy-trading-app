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

from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
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


def _order_reasons(db: Session, order_ids: list) -> dict:
    """Why each order was placed: a recorded reason (services/position_events),
    else the Discord alert that placed it."""
    out: dict = {}
    if not order_ids:
        return out
    try:
        for e in db.execute(select(PositionEvent).where(
                PositionEvent.kind == "order_note", PositionEvent.order_id.in_(order_ids))).scalars():
            out.setdefault(e.order_id, e.note)
    except Exception:  # noqa: BLE001 — a database without the column yet
        pass
    try:
        from app.models.discord_alert_source import DiscordAlertSource  # noqa: PLC0415
        from app.models.discord_message import DiscordMessage  # noqa: PLC0415

        rows = db.execute(
            select(DiscordMessage.order_id, DiscordMessage.content, DiscordAlertSource.label,
                   DiscordAlertSource.channel_id)
            .join(DiscordAlertSource, DiscordAlertSource.id == DiscordMessage.source_id)
            .where(DiscordMessage.order_id.in_(order_ids))
        ).all()
        for oid, content, label, channel in rows:
            if oid in out:
                continue
            text = " ".join((content or "").split())
            text = text if len(text) <= 120 else text[:117] + "…"
            who = "typed in the Discord popup" if channel == "self" else label
            out[oid] = f"{who} alert: “{text}”"
    except Exception:  # noqa: BLE001
        pass
    return out


_EVENT_LABELS = {
    "stop_set": "Stop set",
    "stop_moved": "Stop moved",
    "stop_removed": "Stop removed",
    "trailing_stop_set": "Trailing stop set",
    "trailing_stop_raised": "Trailing stop raised",
    "trailing_exit_armed": "Trailing exit armed",
    "trailing_exit_cleared": "Trailing exit cleared",
    "ladder_closed": "Ladder finished",
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
    if e.kind == "ladder_closed":
        return ""
    moved = f"{_s(e.old_price)} → {_s(e.price)}" if e.old_price is not None else f"@ {_s(e.price)}"
    return moved + (f" · {trail}" if trail else "")


def _fallback_reason(o: Order, filled: bool) -> str | None:
    """When nothing recorded why: what the order itself says."""
    if o.order_type in _STOP_TYPES and filled:
        return "stop order filled at the broker"
    if o.bracket_leg == "sl":
        return "the entry's stop-loss"
    if o.bracket_leg == "tp":
        return "the entry's take-profit"
    if o.parent_order_id is not None:
        return "copied from the trader you follow"
    return None


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

    # Each sell's realized P&L as the rest of the app reports it (Closed today,
    # Order History): a FIFO walk over the user's whole history. Falls back to
    # the sell against this summary's average cost when it has no figure.
    try:
        from app.services.pnl import realized_pnl_by_order  # noqa: PLC0415

        realized = realized_pnl_by_order(db, user_id)
    except Exception:  # noqa: BLE001
        realized = {}

    reasons = _order_reasons(db, [o.id for o in orders])

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
        pnl = None
        if mine and buy:
            buys += 1
            label = "Entry" if buys == 1 else ("Average" if price is not None and avg and price < avg else "Add")
            if price is not None:
                avg = ((avg * held) + price * filled) / (held + filled) if held + filled else price
            held += filled
        elif mine:
            # What this sell realized: the order's own figure (what Closed today
            # shows), else the fill against the average cost it sold from.
            if o.id in realized:
                pnl = Decimal(str(realized[o.id]))
            elif price is not None and avg:
                mult = Decimal(100) if o.instrument_type == InstrumentType.OPTION else Decimal(1)
                pnl = (price - avg) * filled * mult
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
            # Realized P&L of a sell, in dollars (signed, 2 decimals).
            "pnl": (f"{pnl.quantize(Decimal('0.01')):f}" if pnl is not None else None),
            # Why it was placed: the alert, auto-trim, a stop, you on Positions …
            "note": reasons.get(o.id) or _fallback_reason(o, mine),
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
        if e.kind == "order_note" or not inside(e.created_at):
            continue                      # an order's reason is shown on its own line
        at = _utc(e.created_at)
        items.append((at, {
            "type": "event",
            "at": at.isoformat(),
            "label": _EVENT_LABELS.get(e.kind, e.kind),
            "side": None, "requested": None, "filled": None, "status": None, "remaining": None,
            "avg_price": None, "pnl": None,
            "note": getattr(e, "note", None),
            "detail": _event_detail(e),
        }))

    items.sort(key=lambda it: (it[0] or datetime.min.replace(tzinfo=timezone.utc)))
    return [i for _, i in items]


__all__ = ["holding", "timeline"]


_EXIT_MODES = {
    "alerts": "on exit alerts",
    "auto": "auto-trim",
    "orders": "take-profit orders",
    "manual": "manual — nothing sells on its own",
}


def rules(db: Session, user_id, symbol: str, *, strike: Decimal | None = None,
          right: str | None = None, expiry: date | None = None) -> dict | None:
    """The settings that govern this position: whose they are, how exits work,
    the ladder and how far along it the position is. None for a position with
    no Discord ladder (opened by hand, never assigned a channel)."""
    from sqlalchemy import desc  # noqa: PLC0415

    from app.models.discord_alert_source import DiscordAlertSource  # noqa: PLC0415
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415
    from app.models.settings import TraderSettings  # noqa: PLC0415
    from app.services import discord_channel_settings as dcs  # noqa: PLC0415
    from app.services import discord_ladder  # noqa: PLC0415

    q = select(DiscordPositionGuard).where(
        DiscordPositionGuard.user_id == user_id,
        DiscordPositionGuard.symbol == symbol.upper(),
    )
    for col, val in ((DiscordPositionGuard.option_strike, strike),
                     (DiscordPositionGuard.option_right, right),
                     (DiscordPositionGuard.option_expiry, expiry)):
        q = q.where(col.is_(None) if val is None else col == val)
    # The live one, else the most recent (a closed position's).
    guard = db.execute(q.order_by(DiscordPositionGuard.closed_at.isnot(None),
                                  desc(DiscordPositionGuard.created_at)).limit(1)).scalars().first()
    if guard is None:
        return None

    source_id = guard.source_id or dcs.source_for_order(db, guard.entry_order_id)
    src = db.get(DiscordAlertSource, source_id) if source_id else None
    ts = dcs.for_guard(db, user_id, guard) or db.get(TraderSettings, user_id)
    if ts is None:
        return None
    if src is None:
        channel = "account"
    elif src.channel_id == "self":
        channel = "Self"
    elif src.use_account_settings:
        channel = f"{src.label}, account settings"
    else:
        channel = src.label

    trails = discord_ladder.stop_trails(ts)
    done = guard.sell_count or 0
    ladder = []
    for i, r in enumerate(discord_ladder.rungs(ts), start=1):
        stop = (f"trail {_s(abs(r.stop_pct))}%" if r.stop_pct is not None and trails[i - 1]
                else f"stop {_s(r.stop_pct)}%")
        ladder.append({
            "trim": i,
            "target": f"+{_s(r.profit_gate_pct)}%" if r.profit_gate_pct else "any",
            "sells": f"sell {_s(r.qty_pct)}%",
            "stop": stop,
            "state": "done" if i <= done else ("next" if i == done + 1 else ""),
        })
    fill = discord_ladder.fill_stop_pct(ts)
    engine = getattr(ts, "discord_exit_engine", None)
    mult = getattr(ts, "discord_quantity_multiplier", None) or 1
    dollars = getattr(ts, "discord_size_dollars", None)
    by_dollars = getattr(ts, "discord_size_mode", None) == "dollars" and dollars
    max_c = getattr(ts, "discord_max_per_contract", None)
    max_o = getattr(ts, "discord_max_per_order", None)
    return {
        "channel": channel,
        "quantity": (f"${_px(dollars)} per entry" if by_dollars
                     else f"{mult} contract{'s' if mult != 1 else ''} per entry"),
        "max_per_contract": f"${_px(max_c)}" if max_c else None,
        "max_per_order": f"${_px(max_o)}" if max_o else None,
        "exits": "AI trimming" if engine == "ai" else _EXIT_MODES.get(dcs.exit_mode(ts), dcs.exit_mode(ts)),
        "entries": "market" if dcs.entry_order_type(db, source_id) == "market" else "limit",
        "mode": "live" if getattr(ts, "discord_live_trading", False) else "paper",
        "on_fill": (None if fill is None else
                    (f"trail {_s(abs(fill))}%" if discord_ladder.fill_stop_trails(ts)
                     else f"stop {_s(fill)}%")),
        "ladder": ladder,
        "entry_price": _px(guard.entry_price),
        "stop_now": _px(guard.stop_price),
        "trailing_now": (f"{_s(guard.stop_trail_pct)}% below {_px(guard.stop_peak)}"
                         if getattr(guard, "stop_trail_pct", None) is not None else None),
        "closed": guard.closed_reason if guard.closed_at is not None else None,
    }
