"""An alert edited in place is a correction to the trade it already placed.

    $SPY 770 CALL 0DTE @0.20      -> placed, resting unfilled
            (edited seconds later)
    $SPY 770 CALL 0DTE @0.15      -> the SAME order moves to 0.15

Alert channels do this constantly: the author posts a price, then corrects it
once they see their own fill. Until now the edit arrived, hit the message
uniqueness constraint, was counted as a duplicate and thrown away — so our
order sat at a price the author had already withdrawn.

── One trade, not two ───────────────────────────────────────────────────────
The edit never runs through execution. It repoints the order the ORIGINAL
message placed (``DiscordMessage.order_id``), so an edited alert can never open
a second position.

── Only an untouched entry moves ────────────────────────────────────────────
Filled, partially filled, cancelled, a close, a mirror — all refused by name. A
fill cannot be undone, and "cancel the old one and place the new one" only makes
sense while nothing has traded. A partial fill is the sharp case: the position
is real, so the resting remainder is left alone rather than resized under a
ladder that is already measuring against the first fill.

── Held and retried, never dropped ──────────────────────────────────────────
The broker often cannot apply the edit at the moment it arrives. Pre-market an
Alpaca option rests in ``accepted`` — received, not yet routed — and will not
take a PATCH until options start routing at 09:30, which is exactly when an
alert channel is busiest. The wanted price is therefore recorded on the order
(``discord_edit_price``) and retried by ``retry_tick`` until it lands, the entry
fills, or the order goes away. Nothing is cancelled while it waits, so the entry
can never vanish; it simply rests at the old price until the broker will move it.

── Replace, not cancel-then-place ───────────────────────────────────────────
The outcome asked for is "the 0.20 order is gone and 0.15 is resting", and the
broker's own replace delivers exactly that in one step. Cancel-then-place has a
gap in the middle where the trader holds nothing, and if the place then fails
the entry is gone with nothing in its stead — that happened live on Webull (a
4-lot buy cancelled 30s in, the re-place never landed, and every later trim
fired into a position that did not exist). Both brokers we place on support
atomic replace; anywhere else this declines rather than risking that gap.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.discord_message import DiscordMessage
from app.models.order import Order, OrderStatus

log = logging.getLogger(__name__)

_WORKING = (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED)

# The fields that say WHICH contract. If any of these moved, the edit is not a
# price correction — it is a different trade, and repricing our resting order to
# match would silently change what we are buying.
_CONTRACT_KEYS = ("action", "symbol", "asset_type", "strike", "option_type", "expiration")


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def same_contract(before: dict, after: dict) -> bool:
    """Do both readings name the same trade?

    Compared on the stated fields only. A field the edit simply stopped stating
    is treated as unchanged: "$SPY 770 CALL 0DTE @0.20" edited to
    "$SPY 770 CALL @0.15" is the same contract with the expiry left off, not a
    new one — but a field that changed to a DIFFERENT value is disqualifying.
    """
    for key in _CONTRACT_KEYS:
        old, new = before.get(key), after.get(key)
        if new in (None, "") or old in (None, ""):
            continue
        if key == "strike":
            if _dec(old) != _dec(new):
                return False
        elif str(old).upper() != str(new).upper():
            return False
    return True


def apply_price_edit(db: Session, msg: DiscordMessage) -> str:
    """Move the order this message placed to the edited price. Never raises.

    Returns a short outcome for the log. Every refusal is named rather than
    silent, because "the edit did nothing" and "the edit was not seen" look
    identical in an order history and mean very different things.
    """
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415
    from app.services.discord_reprice import _replace  # noqa: PLC0415

    if msg.order_id is None:
        return "no order was placed for this alert"

    order = db.get(Order, msg.order_id)
    if order is None:
        return "order gone"
    if order.parent_order_id is not None:
        # A mirror follows its parent; repointing it directly would desync the
        # subscriber from the trader.
        return "mirror — the parent's edit carries it"
    if order.is_closing:
        return "a close is not repriced — an exit sells what is held"
    if order.status not in _WORKING:
        return f"order already {order.status.value}"
    if (order.filled_quantity or Decimal(0)) > 0:
        # A fill cannot be undone, and resizing the remainder would move the
        # position's cost basis under a ladder already measuring from it.
        return "partially filled — leaving the remainder alone"

    after = msg.parsed_signal or {}
    new_price = _dec(after.get("limit_price"))
    if new_price is None or new_price <= 0:
        return "the edit states no price"
    if order.limit_price is not None and _dec(order.limit_price) == new_price:
        return "same price"

    before = {
        "action": "SELL" if order.is_closing else "BUY",
        "symbol": order.symbol,
        "asset_type": "OPTION" if order.option_strike is not None else "STOCK",
        "strike": order.option_strike,
        "option_type": order.option_right.value if order.option_right else None,
        "expiration": order.option_expiry.isoformat() if order.option_expiry else None,
    }
    if not same_contract(before, after):
        # Deliberately no fallback. Guessing which of the two contracts was
        # meant is the worst outcome available here.
        return "the edit names a different contract — not repriced"

    # Record the intent BEFORE trying, so a broker that refuses right now is a
    # delay rather than a lost correction.
    order.discord_edit_price = new_price
    db.commit()
    return _attempt(db, order)


def _attempt(db: Session, order: Order) -> str:
    """Try to move ``order`` to its pending ``discord_edit_price``.

    Shared by the arrival path and the retry poller so both refuse, log and
    clear on exactly the same terms. Leaves the pending price in place on a
    broker failure — that is what makes it a retry rather than one shot.
    """
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415
    from app.services.discord_reprice import _replace  # noqa: PLC0415

    new_price = _dec(order.discord_edit_price)
    if new_price is None:
        return "nothing pending"

    acct = db.get(BrokerAccount, order.broker_account_id)
    if acct is None:
        return "no account"
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    if not getattr(adapter, "supports_replace", False):
        # Not a transient state — this broker will never replace. Give the
        # pending price up rather than retrying it forever.
        order.discord_edit_price = None
        db.commit()
        return f"{acct.broker} cannot replace atomically — left resting"

    original = order.limit_price
    try:
        _replace(adapter, order, new_price)
    except Exception as exc:  # noqa: BLE001
        # Kept pending on purpose. The commonest cause is the order not being
        # replaceable YET (pre-market `accepted`), which resolves on its own.
        log.warning(
            "discord edit: %s not moved from %s to %s yet — %s",
            order.symbol, original, new_price, exc,
        )
        return f"waiting to apply {new_price}: {str(exc)[:120]}"

    order.limit_price = new_price
    order.discord_edit_price = None
    db.commit()
    log.info(
        "discord edit: %s alert edited %s -> %s; the resting order was moved",
        order.symbol, original, new_price,
    )

    # Carry it to the subscribers' mirrors. They are resting at the price the
    # author has withdrawn, and the replacement is app-originated so no listener
    # will detect the change for us. Best-effort: the trader's own order has
    # already moved, and a propagation failure must not undo that.
    try:
        from app.services.copy_engine import propagate_modify_to_mirrors  # noqa: PLC0415

        propagate_modify_to_mirrors(order.id)
    except Exception:  # noqa: BLE001
        log.exception("discord edit: could not carry %s to the mirrors", order.id)

    return f"repriced {original} -> {new_price}"


# ── the retry loop ──────────────────────────────────────────────────────────
# Deliberately cheap and stateless: the pending price lives on the order row, so
# a restart loses nothing and two ticks racing simply both find it already
# applied.
POLL_INTERVAL_S = 20


def pending_order_ids() -> list:
    """Working, unfilled entries still carrying a price an edit asked for."""
    from app.database import SessionLocal  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    with SessionLocal() as db:
        return list(db.execute(
            select(Order.id).where(
                Order.discord_edit_price.is_not(None),
                Order.status.in_(_WORKING),
                Order.parent_order_id.is_(None),
            )
        ).scalars())


def retry_one(order_id) -> str:
    """One pending edit, in its own session. Never raises."""
    from app.database import SessionLocal  # noqa: PLC0415

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if order is None or order.discord_edit_price is None:
            return "gone"
        # Re-checked under THIS session: the order may have filled or been
        # cancelled since the scan, which is the common case on a busy contract.
        if order.status not in _WORKING or (order.filled_quantity or Decimal(0)) > 0:
            # Give up rather than retrying forever. A fill cannot be undone, and
            # the price the author asked for no longer describes anything we can
            # act on.
            order.discord_edit_price = None
            db.commit()
            return "no longer a working unfilled entry"
        outcome = _attempt(db, order)

    # The arrival path carries the mirrors itself; when the RETRY is what
    # finally landed it, this is the only place that can.
    if outcome.startswith("repriced"):
        try:
            from app.services.copy_engine import propagate_modify_to_mirrors  # noqa: PLC0415

            propagate_modify_to_mirrors(order_id)
        except Exception:  # noqa: BLE001
            log.exception("discord edit: could not carry %s to the mirrors", order_id)
    return outcome


def tick() -> None:
    for oid in pending_order_ids():
        try:
            outcome = retry_one(oid)
            if outcome not in ("gone", "nothing pending"):
                log.info("discord edit retry: order %s -> %s", oid, outcome)
        except Exception:  # noqa: BLE001
            log.exception("discord edit retry: failed on order %s", oid)


def poll_loop(shutdown_check=None) -> None:
    import time  # noqa: PLC0415

    log.info("discord_edit: retry loop starting (interval=%ss)", POLL_INTERVAL_S)
    while True:
        if shutdown_check is not None and shutdown_check():
            return
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("discord_edit: retry tick failed")
        time.sleep(POLL_INTERVAL_S)
