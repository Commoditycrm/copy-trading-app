"""A re-posted entry is a correction, not a second trade.

Authors fix an alert by posting it again — QA 2026-10-06, Clint:

    16:16:29  $SPY 781 CALL 0DTE @0.63, Lotto!
    16:16:59  $SPY 781 CALL 0DTE @0.56, Lotto!      <- a new message, not an edit

An EDITED message already moves the order it placed (services/discord_edit).
A new message carried no such link, so it was traded as a second entry: a
subscriber ended up with two SPY 781C positions. (The trader's own account was
spared only because its second attempt hit a Webull rate limit.)

So an ENTRY from a channel that already placed an entry for the SAME contract
within ``discord_repost_window_s`` (3 minutes) is absorbed into that one:

  * its order is still resting unfilled -> moved to the new price, exactly as
    an edit would move it;
  * it has filled (or is part filled) -> nothing more is bought.

Only plain entries. An averaging-down / "Adding" alert is a deliberate second
buy and is never absorbed; nor is anything from another channel, or a contract
that differs in any stated field. Runs inside _execute_signal, so it covers the
trader and every subscriber (whose alerts arrive on their own mirror channel).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_message import DiscordMessage
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.services import discord_edit

log = logging.getLogger(__name__)

# A previous entry that did not trade does not count: re-posting after a
# refusal or a cancel is how an author gets a trade that failed placed.
_DEAD = (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED)


def _window_s() -> int:
    from app.config import get_settings  # noqa: PLC0415

    return int(get_settings().discord_repost_window_s)


def _is_plain_entry(signal: dict) -> bool:
    return (
        str(signal.get("action") or "").upper() == "BUY"
        and not signal.get("double_up")
        and not signal.get("add_to_latest")
        and bool(signal.get("symbol"))
    )


def _before(order: Order) -> dict:
    return {
        "action": "BUY",
        "symbol": order.symbol,
        "asset_type": "OPTION" if order.option_strike is not None else "STOCK",
        "strike": order.option_strike,
        "option_type": order.option_right.value if order.option_right else None,
        "expiration": order.option_expiry.isoformat() if order.option_expiry else None,
    }


def find_recent_entry(db: Session, msg, signal: dict, *, now: datetime | None = None,
                      window_s: int | None = None) -> tuple[DiscordMessage, Order] | None:
    """The entry this same channel placed for the same contract within the
    window, if ``signal`` is a plain entry and there is one."""
    if not _is_plain_entry(signal) or getattr(msg, "source_id", None) is None:
        return None
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(seconds=window_s if window_s is not None else _window_s())
    rows = db.execute(
        select(DiscordMessage, Order)
        .join(Order, Order.id == DiscordMessage.order_id)
        .where(
            DiscordMessage.source_id == msg.source_id,
            DiscordMessage.user_id == msg.user_id,
            DiscordMessage.id != msg.id,
            DiscordMessage.created_at >= since,
            Order.side == OrderSide.BUY,
            Order.is_closing.is_(False),
            Order.status.notin_(_DEAD),
            Order.symbol == str(signal.get("symbol")).upper(),
        )
        .order_by(DiscordMessage.created_at.desc())
    ).all()
    for prior_msg, order in rows:
        if not _is_plain_entry(prior_msg.parsed_signal or {}):
            continue
        if discord_edit.same_contract(_before(order), signal):
            return prior_msg, order
    return None


def absorb(db: Session, msg, prior_msg: DiscordMessage, order: Order, signal: dict,
           *, now: datetime | None = None) -> str:
    """Fold a re-posted entry into the order the first one placed. Returns the
    reason recorded on the new alert."""
    now = now or datetime.now(timezone.utc)
    then = prior_msg.created_at
    if then is not None and then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    ago = f"{int((now - then).total_seconds())}s ago" if then is not None else "moments ago"
    lead = f"Re-posted entry — the same {order.symbol} contract this channel bought {ago}"

    filled = Decimal(str(order.filled_quantity or 0))
    if filled > 0:
        px = f" @ {order.filled_avg_price}" if order.filled_avg_price is not None else ""
        return f"{lead} is already filled ({filled.normalize():f}{px}); not bought again."
    new_price = discord_edit._dec(signal.get("limit_price"))
    if (order.status in discord_edit._WORKING and order.order_type == OrderType.LIMIT
            and new_price is not None and new_price > 0
            and discord_edit._dec(order.limit_price) != new_price):
        # The author corrected the price: move the resting order, as an edit does.
        order.discord_edit_price = new_price
        db.commit()
        outcome = discord_edit._attempt(db, order)
        return f"{lead} is still resting — {outcome}; not bought again."
    return f"{lead} is still working; not bought again."


__all__ = ["find_recent_entry", "absorb"]
