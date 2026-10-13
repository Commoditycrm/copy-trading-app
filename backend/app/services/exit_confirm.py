"""Did a sell actually go through?

A market exit is usually FILLED by the time placement returns — but not always.
The broker can accept the order and report the fill a moment later, and the
order row then still reads SUBMITTED. That matters because the stop on what is
left waits on the answer:

  * confirmed filled — the contracts are gone, the remainder is free, and a
    correctly sized stop can go out at once;
  * not yet — poking the stop reconciler now would size the stop to the WHOLE
    position (the broker still reports every contract held) and that stop would
    reserve the very contracts the sell is trying to take, which the broker
    refuses as opening a naked position.

So the fill is confirmed before anything is put back, and it is checked more
than once: a single read a fraction of a second after placement is the case
that reports "not filled" for an exit that fills immediately afterwards, and
then protection waits for the next poll tick instead of going out now.

Read from the BROKER rather than the local row: fills reach the database through
the listeners and the fill sync, which are exactly what this is trying not to
wait for.

Never raises. An exit that cannot be confirmed falls back to the behaviour that
was there before — the poller's next tick places the stop — so a failure here
can only make protection later, never absent.
"""
from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Two reads, because one is the known failure: a market exit reported as
# accepted and filled milliseconds later.
ATTEMPTS = 2
# Long enough for a market option fill to be reported, short enough that a trim
# does not sit here. Both reads together add well under two seconds.
PAUSE_S = 0.75

_FILLED_STATES = {"filled", "partially_filled"}


def _filled_quantity(result: Any) -> Decimal:
    for name in ("filled_quantity", "filled_qty", "filledQuantity", "cumulative_quantity"):
        raw = getattr(result, name, None)
        if raw is None and isinstance(result, dict):
            raw = result.get(name)
        if raw in (None, ""):
            continue
        try:
            got = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if got > 0:
            return got
    return Decimal(0)


def _is_filled(result: Any) -> bool:
    if _filled_quantity(result) > 0:
        return True
    status = getattr(result, "status", None)
    status = getattr(status, "value", status)
    return str(status or "").lower() in _FILLED_STATES


def record_fill_details(order: Any, result: Any) -> None:
    """Copy a filled broker order's price and execution time onto our row, and
    stamp the close time. Fills only what is missing — never overwrites."""
    from datetime import datetime, timezone  # noqa: PLC0415

    px = getattr(result, "filled_avg_price", None)
    if px is not None and getattr(order, "filled_avg_price", None) is None:
        try:
            if Decimal(str(px)) > 0:
                order.filled_avg_price = Decimal(str(px))
        except Exception:  # noqa: BLE001
            pass
    at = getattr(result, "filled_at", None)
    if at is not None and getattr(order, "broker_filled_at", None) is None:
        order.broker_filled_at = at
    if getattr(order, "closed_at", None) is None:
        order.closed_at = at or datetime.now(timezone.utc)


def confirm_filled(
    db: Session,
    broker_account_id: uuid.UUID | None,
    order: Any,
    *,
    attempts: int = ATTEMPTS,
    pause_s: float = PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Did this sell take contracts? Confirmed against the broker.

    Returns True only on a fill we have actually seen — never optimistically.
    An order already FILLED locally is taken at its word and costs no broker
    call; anything else is re-read ``attempts`` times.
    """
    from app.models.order import OrderStatus  # noqa: PLC0415

    if order is None:
        return False
    if getattr(order, "status", None) is OrderStatus.FILLED:
        return True
    broker_order_id = getattr(order, "broker_order_id", None)
    if not broker_order_id or broker_account_id is None:
        return False

    try:
        from app.brokers import adapter_for  # noqa: PLC0415
        from app.models.broker_account import BrokerAccount  # noqa: PLC0415
        from app.services.crypto import decrypt_json  # noqa: PLC0415

        acct = db.get(BrokerAccount, broker_account_id)
        if acct is None:
            return False
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    except Exception:  # noqa: BLE001
        log.warning("exit_confirm: could not reach the broker for order %s",
                    getattr(order, "id", "?"), exc_info=True)
        return False

    for attempt in range(1, max(1, attempts) + 1):
        if pause_s > 0:
            sleep(pause_s)
        try:
            result = adapter.get_order(str(broker_order_id))
        except Exception:  # noqa: BLE001
            log.warning("exit_confirm: read %s of order %s failed", attempt,
                        getattr(order, "id", "?"), exc_info=True)
            continue
        if not _is_filled(result):
            continue
        # Record what we learned, so whatever sizes the next stop reads the
        # same thing the broker just told us.
        got = _filled_quantity(result)
        if got > 0:
            order.filled_quantity = got
        order.status = OrderStatus.FILLED
        # …and the price and time, from the same read. Without them the sell
        # was FILLED with no price and no close time: Closed today (which picks
        # a day's sells by fill/close time) and realized P&L both left it out,
        # and on a Discord subscriber's Webull account nothing else ever came
        # back to fill them in (QA 2026-10-13, SPY 776P stop-out).
        record_fill_details(order, result)
        try:
            db.flush()
        except Exception:  # noqa: BLE001
            log.warning("exit_confirm: could not record the fill for %s",
                        getattr(order, "id", "?"), exc_info=True)
        log.info("exit_confirm: sell %s confirmed filled on read %s (%s)",
                 getattr(order, "id", "?"), attempt, got or "quantity unknown")
        return True

    log.info("exit_confirm: sell %s not confirmed after %s reads — the stop goes "
             "out on the next poll tick", getattr(order, "id", "?"), attempts)
    return False


__all__ = ["ATTEMPTS", "PAUSE_S", "confirm_filled"]
