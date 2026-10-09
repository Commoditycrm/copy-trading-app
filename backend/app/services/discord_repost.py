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

── A switched contract ──────────────────────────────────────────────────────
The same channel calling the same ticker on a DIFFERENT option contract within
the window (781C -> 780C, or calls -> puts) means the author changed their mind:

  * the first entry is still resting unfilled -> it is cancelled and the new
    contract is traded;
  * it has filled -> the new contract is traded too, and the trader is told
    they still hold the first one. Nothing is sold automatically: whether to
    keep a filled position is not something an alert can say by switching.

An EDIT that switches the contract is the same case on the same message
(:func:`switch_on_edit`). Unfilled -> cancel and trade the edited contract.
Filled -> notify only: the message already carries the filled order, and
re-pointing it at a new one would cut the position off from its channel.

── A deleted alert ──────────────────────────────────────────────────────────
The author deleting an entry withdraws it (:func:`apply_delete`): a still
resting unfilled entry is cancelled; a filled one is flagged, never sold.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
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


def _recent_entries(db: Session, msg, symbol: str, *, now: datetime | None,
                    window_s: int | None) -> list[tuple[DiscordMessage, Order]]:
    """This channel's live plain entries on ``symbol`` within the window, newest
    first — every entry order a message other than ``msg`` placed."""
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
            Order.symbol == symbol.upper(),
        )
        .order_by(DiscordMessage.created_at.desc())
    ).all()
    return [(m, o) for m, o in rows if _is_plain_entry(m.parsed_signal or {})]


def find_recent_entry(db: Session, msg, signal: dict, *, now: datetime | None = None,
                      window_s: int | None = None) -> tuple[DiscordMessage, Order] | None:
    """The entry this same channel placed for the same contract within the
    window, if ``signal`` is a plain entry and there is one."""
    if not _is_plain_entry(signal) or getattr(msg, "source_id", None) is None:
        return None
    for prior_msg, order in _recent_entries(db, msg, str(signal.get("symbol")), now=now, window_s=window_s):
        if discord_edit.same_contract(_before(order), signal):
            return prior_msg, order
    return None


def find_switched_entry(db: Session, msg, signal: dict, *, now: datetime | None = None,
                        window_s: int | None = None) -> tuple[DiscordMessage, Order] | None:
    """The entry this same channel placed for a DIFFERENT option contract on the
    same ticker within the window — the call ``signal`` replaces — if any.

    Options only: an option and the stock on one ticker are two trades, not a
    correction of one another."""
    if (not _is_plain_entry(signal) or getattr(msg, "source_id", None) is None
            or str(signal.get("asset_type") or "OPTION").upper() != "OPTION"):
        return None
    for prior_msg, order in _recent_entries(db, msg, str(signal.get("symbol")), now=now, window_s=window_s):
        if order.option_strike is None:
            continue
        if not discord_edit.same_contract(_before(order), signal):
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


# ── switches and deletions ──────────────────────────────────────────────────

# Every deleted alert's reason starts with this, which is also how a deletion
# replayed by a reconnecting listener is recognised as already handled.
DELETED_PREFIX = "Deleted in Discord"


def _label(order: Order) -> str:
    if order.option_strike is None:
        return order.symbol
    right = (order.option_right.value[:1].upper() if order.option_right else "")
    exp = f" {order.option_expiry.month}/{order.option_expiry.day}" if order.option_expiry else ""
    return f"{order.symbol} {Decimal(str(order.option_strike)).normalize():f}{right}{exp}"


def _signal_label(signal: dict) -> str:
    sym = str(signal.get("symbol") or "").upper()
    strike = discord_edit._dec(signal.get("strike"))
    if strike is None:
        return sym
    right = str(signal.get("option_type") or "")[:1].upper()
    return f"{sym} {strike.normalize():f}{right}"


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "moments ago"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return f"{int((now - then).total_seconds())}s ago"


def _filled(order: Order) -> Decimal:
    return Decimal(str(order.filled_quantity or 0))


def _notify(db: Session, user_id, message: str, **meta) -> None:
    """Best-effort: the outcome is already recorded on the alert."""
    try:
        from app.services.notifications import create_notification  # noqa: PLC0415

        create_notification(db, user_id=user_id, type="discord.alert_changed",
                            message=message[:480], metadata={k: str(v) for k, v in meta.items()})
    except Exception:  # noqa: BLE001
        log.exception("discord: could not notify %s — %s", user_id, message)


def _fanout(order_id, background) -> None:
    """Cancel the copy mirrors resting on the same entry, as every other entry
    cancel does."""
    from app.api.trades import _run_cancel_fanout_in_background  # noqa: PLC0415

    if background is not None:
        background.add_task(_run_cancel_fanout_in_background, order_id)
    else:
        _run_cancel_fanout_in_background(order_id)


def cancel_entry(db: Session, order: Order) -> str | None:
    """Cancel one resting, unfilled entry. None when it is cancelled, otherwise
    why it was not.

    Only marked cancelled when the broker cancelled it, or it never reached one:
    a broker that says the order already finished may well have FILLED it, and
    recording that as cancelled would hide a real position."""
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    if order.status not in discord_edit._WORKING:
        return f"it is already {order.status.value}"
    if _filled(order) > 0:
        return "it is part filled"
    if order.broker_order_id:
        acct = db.get(BrokerAccount, order.broker_account_id)
        if acct is None:
            return "its broker account is gone"
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            done = adapter.cancel_order(order.broker_order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("discord: could not cancel entry %s", order.id, exc_info=True)
            return f"the broker refused the cancel: {str(exc)[:120]}"
        if done is False:
            return "the broker says it already finished — it may have filled"
    order.status = OrderStatus.CANCELED
    order.closed_at = datetime.now(timezone.utc)
    db.commit()
    return None


def supersede(db: Session, msg, prior_msg: DiscordMessage, order: Order, signal: dict,
              *, background=None, now: datetime | None = None) -> str:
    """The channel switched from ``order``'s contract to ``signal``'s. Cancel the
    first entry if it never filled; otherwise tell the trader they still hold it.
    The caller goes on to trade ``signal`` either way. Returns what happened."""
    now = now or datetime.now(timezone.utc)
    old, new = _label(order), _signal_label(signal)
    ago = _ago(prior_msg.created_at, now)
    if _filled(order) == 0 and order.status in discord_edit._WORKING:
        why = cancel_entry(db, order)
        if why is None:
            _fanout(order.id, background)
            prior_msg.status_reason = (
                f"Superseded — the channel switched to {new}; this unfilled entry was cancelled."
            )[:480]
            return f"switched from {old} ({ago}) to {new}; the unfilled {old} entry was cancelled"
        _notify(db, msg.user_id,
                f"The channel switched from {old} to {new}, but the earlier {old} entry "
                f"could not be cancelled ({why}). Check it — {new} is being placed.",
                order_id=order.id)
        return f"switched from {old} to {new}; the {old} entry could not be cancelled ({why})"
    held = f"{_filled(order).normalize():f}"
    _notify(db, msg.user_id,
            f"The channel switched from {old} to {new} {ago}. You still hold {held} {old} — "
            f"not sold automatically. {new} is being placed as a new trade.",
            order_id=order.id)
    prior_msg.status_reason = (
        f"Superseded — the channel switched to {new}; this entry had filled and is still held."
    )[:480]
    return f"switched from {old} to {new}; {held} {old} still held — not sold automatically"


def switch_on_edit(db: Session, msg: DiscordMessage, *, background=None,
                   now: datetime | None = None) -> tuple[str, bool]:
    """An edit changed WHICH contract the alert names. Returns the outcome, and
    whether the caller should now trade the edited contract.

    Unfilled -> the old entry is cancelled and the message is freed to trade the
    edited contract (in manual mode it goes back to awaiting approval). Filled,
    or the edit came long after the alert -> flagged only."""
    now = now or datetime.now(timezone.utc)
    order = db.get(Order, msg.order_id) if msg.order_id is not None else None
    signal = msg.parsed_signal or {}
    if order is None or order.is_closing or order.parent_order_id is not None or not _is_plain_entry(signal):
        return discord_edit.DIFFERENT_CONTRACT, False
    old, new = _label(order), _signal_label(signal)
    then = msg.created_at
    if then is not None and then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    late = then is not None and (now - then).total_seconds() > _window_s()

    if _filled(order) == 0 and order.status in discord_edit._WORKING and not late:
        why = cancel_entry(db, order)
        if why is not None:
            _notify(db, msg.user_id,
                    f"An alert was edited from {old} to {new}, but the {old} entry could not be "
                    f"cancelled ({why}). {new} was not placed.", order_id=order.id)
            return f"switched to {new}, but the {old} entry could not be cancelled ({why})", False
        _fanout(order.id, background)
        msg.order_id = None
        msg.status = DiscordMessageStatus.PARSED
        trade = msg.decision is SignalDecision.APPROVED
        return (f"switched from {old} to {new}; the unfilled {old} entry was cancelled"
                + ("" if trade else " — the new contract awaits your approval")), trade

    if _filled(order) > 0:
        held = f"{_filled(order).normalize():f}"
        _notify(db, msg.user_id,
                f"An alert you're in was edited from {old} to {new}. You still hold {held} {old} — "
                f"not sold, and {new} not bought automatically.", order_id=order.id)
        return f"switched to {new}; {held} {old} already filled and still held — {new} not traded", False
    why = "the edit came too long after the alert" if late else f"the {old} entry is {order.status.value}"
    return f"switched to {new}; {why} — not traded automatically", False


def _carried_by_repost(db: Session, msg: DiscordMessage, order: Order) -> bool:
    """Did a later alert from this channel re-post the same contract? Then the
    order is that alert's now (services/discord_repost.absorb moved it to the new
    price), and deleting the original — the usual "delete and re-post" — must
    not cancel it."""
    then = msg.created_at
    if then is None:
        return False
    rows = db.execute(
        select(DiscordMessage).where(
            DiscordMessage.source_id == msg.source_id,
            DiscordMessage.user_id == msg.user_id,
            DiscordMessage.id != msg.id,
            DiscordMessage.created_at >= then,
            DiscordMessage.created_at <= then + timedelta(seconds=_window_s()),
        )
    ).scalars()
    for later in rows:
        if (later.status_reason or "").startswith(DELETED_PREFIX):
            continue
        sig = later.parsed_signal or {}
        if _is_plain_entry(sig) and discord_edit.same_contract(_before(order), sig):
            return True
    return False


def apply_delete(db: Session, msg: DiscordMessage, *, background=None) -> str:
    """The author deleted this alert. Cancel its entry if that is still resting
    unfilled; if it filled, tell the trader and sell nothing. Returns what
    happened."""
    if msg.order_id is None:
        return "no order had been placed for it"
    order = db.get(Order, msg.order_id)
    if order is None:
        return "its order is gone"
    if order.side != OrderSide.BUY or order.is_closing:
        return "an exit — left as it is"
    if order.parent_order_id is not None:
        return "a mirror — it follows its parent"
    label = _label(order)
    if _carried_by_repost(db, msg, order):
        return f"a later alert from this channel re-posted {label} — the order stays with it"
    if _filled(order) == 0 and order.status in discord_edit._WORKING:
        why = cancel_entry(db, order)
        if why is None:
            _fanout(order.id, background)
            return f"the unfilled {label} entry was cancelled"
        _notify(db, msg.user_id,
                f"A {label} alert was deleted in Discord, but its entry could not be cancelled ({why}).",
                order_id=order.id)
        return f"the {label} entry could not be cancelled ({why})"
    if _filled(order) > 0:
        held = f"{_filled(order).normalize():f}"
        _notify(db, msg.user_id,
                f"The {label} alert you're in was deleted in Discord. You still hold {held} — "
                "not sold automatically.", order_id=order.id)
        return f"{held} {label} already filled and still held — not sold automatically"
    return f"its order was already {order.status.value}"


__all__ = [
    "DELETED_PREFIX", "find_recent_entry", "find_switched_entry", "absorb",
    "cancel_entry", "supersede", "switch_on_edit", "apply_delete",
]
