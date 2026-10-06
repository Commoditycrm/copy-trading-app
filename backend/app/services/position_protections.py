"""What is protecting each held position: stops, trailing stops, take-profits.

Shown on the Positions page as small icons per row, with the details underneath
on click. Gathered from the three places a protection can live:

  * the Discord ladder's guard — its stop (fixed or trailing, resting at the
    broker or watched by the app), its resting take-profit, and a trailing exit
    on a slice (also what the Positions page's own trailing stop arms);
  * working exit orders at the broker for the contract — a stop, stop-limit,
    trailing stop or limit sell placed any other way (trade panel, a native
    trailing stop, a bracket's real legs);
  * the entry's bracket stop-loss / take-profit, where nothing rests for it at
    the broker and the app watches the price instead.

Each order is listed once. Read-only and best-effort: a display column, so the
caller isolates failures.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.broker_account import BrokerAccount
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import Order, OrderSide, OrderStatus, OrderType

log = logging.getLogger(__name__)

STOP = "stop"
TRAILING_STOP = "trailing_stop"
TAKE_PROFIT = "take_profit"

_WORKING = (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
_EXIT_TYPES = {
    OrderType.STOP: STOP,
    OrderType.STOP_LIMIT: STOP,
    OrderType.TRAILING_STOP: TRAILING_STOP,
    OrderType.LIMIT: TAKE_PROFIT,
}


def _right(v) -> str | None:
    return (getattr(v, "value", v) or None) if v is not None else None


def _key(symbol, strike, right, expiry) -> tuple:
    return ((symbol or "").upper(), Decimal(str(strike)) if strike is not None else None, _right(right), expiry)


def _s(v) -> str | None:
    return None if v is None else f"{Decimal(str(v)).normalize():f}"


def _item(kind: str, price, *, quantity=None, where: str = "app", order_id=None, source: str,
          note: str | None = None, trail_pct=None, trail_amount=None, peak=None) -> dict[str, Any]:
    return {
        "kind": kind, "price": _s(price), "quantity": _s(quantity), "where": where,
        "order_id": str(order_id) if order_id else None, "source": source, "note": note,
        "trail_pct": _s(trail_pct), "trail_amount": _s(trail_amount), "peak": _s(peak),
    }


def attach(db: Session, user_id, positions: list) -> None:
    """Set ``.protections`` (a list of dicts, see _item) on every position."""
    for p in positions:
        p.protections = []
    symbols = {(p.symbol or "").upper() for p in positions if p.symbol}
    if not symbols:
        return
    by_pos = {_key(p.symbol, p.option_strike, p.option_right, p.option_expiry): p for p in positions}
    brokers = {
        a.id: str(getattr(a.broker, "value", a.broker)).capitalize()
        for a in db.execute(select(BrokerAccount).where(BrokerAccount.user_id == user_id)).scalars()
    }

    orders = {
        o.id: o for o in db.execute(
            select(Order).where(
                Order.user_id == user_id,
                Order.symbol.in_(symbols),
                Order.status.in_(_WORKING),
                Order.order_type.in_(tuple(_EXIT_TYPES)),
            )
        ).scalars()
    }
    shown: set = set()

    def at(o: Order | None) -> str:
        return brokers.get(o.broker_account_id, "the broker") if o is not None else "app"

    def working(oid) -> Order | None:
        return orders.get(oid) if oid else None

    # ── the Discord ladder ───────────────────────────────────────────────────
    for g in db.execute(
        select(DiscordPositionGuard).where(
            DiscordPositionGuard.user_id == user_id,
            DiscordPositionGuard.closed_at.is_(None),
            DiscordPositionGuard.symbol.in_(symbols),
        )
    ).scalars():
        p = by_pos.get(_key(g.symbol, g.option_strike, g.option_right, g.option_expiry))
        if p is None:
            continue
        tp = working(g.tp_order_id)
        if g.stop_price is not None:
            rest = working(g.stop_order_id) or working(g.tp_stop_order_id)
            if rest is not None:
                shown.add(rest.id)
            trailing = g.stop_trail_pct is not None
            p.protections.append(_item(
                TRAILING_STOP if trailing else STOP, g.stop_price,
                quantity=rest.quantity if rest is not None else None,
                where=at(rest), order_id=rest.id if rest is not None else None, source="ladder",
                note="linked to the take-profit" if rest is not None and rest.id == g.tp_stop_order_id else None,
                trail_pct=g.stop_trail_pct, peak=g.stop_peak if trailing else None,
            ))
        if tp is not None:
            shown.add(tp.id)
            p.protections.append(_item(
                TAKE_PROFIT, tp.limit_price, quantity=tp.quantity, where=at(tp), order_id=tp.id,
                source="ladder", note=f"Trim {g.tp_rung}" if g.tp_rung else None,
            ))
        if g.trail_qty is not None and (g.trail_amount or 0) > 0:
            peak = Decimal(str(g.peak_price)) if g.peak_price is not None else None
            p.protections.append(_item(
                TRAILING_STOP, (peak - Decimal(str(g.trail_amount))) if peak is not None else None,
                quantity=g.trail_qty, source="ladder", trail_pct=g.trail_percent,
                trail_amount=g.trail_amount, peak=peak,
            ))

    # ── any other exit order working at the broker ─────────────────────────
    for o in orders.values():
        if o.id in shown:
            continue
        p = by_pos.get(_key(o.symbol, o.option_strike, o.option_right, o.option_expiry))
        if p is None or p.broker_account_id != o.broker_account_id:
            continue
        exit_side = OrderSide.SELL if Decimal(str(p.quantity)) > 0 else OrderSide.BUY
        if o.side != exit_side:
            continue
        kind = _EXIT_TYPES[o.order_type]
        if kind == TAKE_PROFIT and not o.is_closing and o.bracket_leg != "tp":
            continue                      # a plain limit sell is not shown as a target
        shown.add(o.id)
        p.protections.append(_item(
            kind, o.stop_price if kind != TAKE_PROFIT else o.limit_price,
            quantity=o.quantity, where=at(o), order_id=o.id,
            source="bracket" if o.bracket_leg else "order",
            trail_pct=getattr(o, "trail_percent", None), trail_amount=getattr(o, "trail_price", None),
        ))

    # ── the entry's bracket, where the app watches it ───────────────────────
    for p in positions:
        if Decimal(str(p.quantity or 0)) <= 0:
            continue
        q = select(Order).where(
            Order.user_id == user_id,
            Order.broker_account_id == p.broker_account_id,
            Order.symbol == (p.symbol or "").upper(),
            Order.status == OrderStatus.FILLED,
            Order.is_closing.is_(False),
            Order.bracket_parent_id.is_(None),
            (Order.stop_loss_price.isnot(None)) | (Order.take_profit_price.isnot(None)),
        ).order_by(desc(Order.created_at)).limit(1)
        if p.option_expiry is not None:
            q = q.where(Order.option_expiry == p.option_expiry)
        if p.option_strike is not None:
            q = q.where(Order.option_strike == p.option_strike)
        if p.option_right is not None:
            q = q.where(Order.option_right == p.option_right)
        entry = db.execute(q).scalar_one_or_none()
        if entry is None:
            continue
        legs = {o.bracket_leg for o in orders.values() if o.bracket_parent_id == entry.id}
        kinds = {i["kind"] for i in p.protections}
        if entry.stop_loss_price is not None and "sl" not in legs and STOP not in kinds:
            p.protections.append(_item(STOP, entry.stop_loss_price, source="bracket", note="entry's SL"))
        if entry.take_profit_price is not None and "tp" not in legs and TAKE_PROFIT not in kinds:
            p.protections.append(_item(TAKE_PROFIT, entry.take_profit_price, source="bracket", note="entry's TP"))


__all__ = ["attach", "STOP", "TRAILING_STOP", "TAKE_PROFIT"]
