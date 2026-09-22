"""Give an unfilled Discord entry one more attempt at a higher price.

A Discord buy is placed as a LIMIT at the price the alert named. That is the
right default — it never pays through a wide spread — but it has a failure mode
the trader feels directly: the contract moves while the order rests, the limit
never fills, and a good alert becomes a missed trade.

So an entry that is still working after ``discord_reprice_after_seconds`` gets
ONE repriced attempt, ``discord_reprice_pct`` above its ORIGINAL limit.

Three decisions worth stating, because each rules out something tempting:

  * Above the ORIGINAL limit, not the current ask. Chasing the ask fills more
    often but is unbounded — a contract that ran 300% would be bought at 300%.
    Anchoring to the alert's own price caps what a retry can cost.
  * ONE attempt. ``orders.discord_repriced_at`` is stamped in the same
    transaction as the reprice, and only NULL rows are picked up, so a slow fill
    cannot be walked up indefinitely by repeated ticks.
  * The max-per-contract ceiling still applies. If the higher price breaches it,
    the order is CANCELLED rather than filled — the ceiling means "contracts this
    expensive aren't for me", and it should not be quietly overridden by the very
    mechanism meant to get us in.

Distinct from the neighbouring scanners: ``retry_scheduler`` re-places orders the
broker REJECTED, and ``stale_order_canceller`` cancels a SUBSCRIBER's stale
mirror. This one only ever touches a trader's own Discord-originated entries.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.discord_message import DiscordMessage
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.settings import TraderSettings

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
BATCH_SIZE = 50

_WORKING = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _due_order_ids() -> list[uuid.UUID]:
    """Discord entries still working past their trader's reprice delay.

    The delay lives in the join so one query covers every trader. Partially
    filled orders are included on purpose — the rest of the position is still
    missing, which is the thing being fixed.
    """
    started = func.coalesce(Order.submitted_at, Order.created_at)
    threshold = func.now() - func.make_interval(
        0, 0, 0, 0, 0, 0, TraderSettings.discord_reprice_after_seconds
    )
    with SessionLocal() as db:
        return list(
            db.execute(
                select(Order.id)
                .join(TraderSettings, TraderSettings.user_id == Order.user_id)
                .join(DiscordMessage, DiscordMessage.order_id == Order.id)
                .where(
                    Order.side == OrderSide.BUY,          # entries only
                    Order.order_type == OrderType.LIMIT,  # nothing else can rest on price
                    Order.is_closing.is_(False),
                    Order.status.in_(_WORKING),
                    Order.broker_order_id.isnot(None),
                    Order.broker_account_id.isnot(None),
                    Order.limit_price.isnot(None),
                    Order.discord_repriced_at.is_(None),  # one attempt, ever
                    started <= threshold,
                )
                .order_by(started.asc())
                .limit(BATCH_SIZE)
            ).scalars()
        )


def _breaches_ceiling(price: Decimal, ts: TraderSettings, is_option: bool) -> bool:
    """Whether a single contract at ``price`` costs more than the trader allows.

    Mirrors the entry-side check exactly: the test is on ONE contract's value
    (premium x 100 for an option), not the order's total.
    """
    cap = getattr(ts, "discord_max_per_contract", None)
    if cap is None:
        return False
    per_contract = price * Decimal(100) if is_option else price
    return per_contract > Decimal(str(cap))


def reprice_one(order_id: uuid.UUID) -> str:
    """Reprice or cancel one entry, in its own session. Returns a short outcome."""
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.models.order import InstrumentType  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        # Re-check under this session: it may have filled between the scan and
        # now, which is the common case on a busy contract.
        if order is None or order.status not in _WORKING:
            return "gone"
        if order.discord_repriced_at is not None:
            return "already"

        acct_check = db.get(BrokerAccount, order.broker_account_id)
        if acct_check is None:
            return "no account"

        # Only reprice where the broker can REPLACE a resting order in one step.
        #
        # Without that, the fallback is cancel-then-place — and if the place
        # fails, the entry is gone with nothing in its stead. That happened on
        # Webull: a 4-lot buy was cancelled 30s after placement, the re-place
        # never landed, and the position the trader thought they held did not
        # exist. Every later trim then fired into nothing.
        #
        # A limit that rests unfilled is recoverable; an entry that vanished is
        # not. So on a broker without atomic replace we leave the order alone.
        from app.brokers import adapter_for as _adapter_for  # noqa: PLC0415
        from app.services.crypto import decrypt_json as _decrypt  # noqa: PLC0415

        probe = _adapter_for(acct_check, _decrypt(acct_check.encrypted_credentials))
        if not getattr(probe, "supports_replace", False):
            order.discord_repriced_at = datetime.now(timezone.utc)
            db.commit()
            log.info(
                "discord reprice: %s cannot replace atomically — leaving %s resting",
                acct_check.broker, order.symbol,
            )
            return "skipped (no atomic replace)"

        ts = db.get(TraderSettings, order.user_id)
        pct = Decimal(str(getattr(ts, "discord_reprice_pct", None) or 10))
        original = Decimal(str(order.limit_price))
        new_price = (original * (Decimal(1) + pct / Decimal(100))).quantize(Decimal("0.01"))

        acct = db.get(BrokerAccount, order.broker_account_id)
        if acct is None:
            return "no account"
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))

        is_option = order.instrument_type == InstrumentType.OPTION
        if ts is not None and _breaches_ceiling(new_price, ts, is_option):
            # Getting filled must not cost more than the trader said a contract
            # is worth. Cancel rather than quietly spend past the ceiling.
            try:
                adapter.cancel_order(order.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("discord reprice: cancel failed for %s — %s", order_id, exc)
                return "cancel failed"
            order.discord_repriced_at = datetime.now(timezone.utc)
            order.status = OrderStatus.CANCELED
            db.commit()
            log.info(
                "discord reprice: %s would breach max-per-contract at %s — cancelled",
                order.symbol, new_price,
            )
            return "cancelled (ceiling)"

        # Stamp BEFORE the broker call. If the call throws after the order was
        # actually accepted, a second tick must not reprice it again — one
        # missed retry is cheaper than an unbounded chase.
        order.discord_repriced_at = datetime.now(timezone.utc)
        db.commit()

        try:
            _replace(adapter, order, new_price)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "discord reprice: %s could not be repriced to %s — %s",
                order.symbol, new_price, exc,
            )
            return "reprice failed"

        order.limit_price = new_price
        db.commit()
        log.info(
            "discord reprice: %s unfilled at %s — retrying at %s (+%s%%)",
            order.symbol, original, new_price, pct,
        )
        return f"repriced to {new_price}"


def _replace(adapter, order: Order, new_price: Decimal) -> None:
    """Move the resting order to ``new_price``.

    Prefers the broker's own replace, which keeps one live order throughout. The
    cancel-then-place fallback has a real gap in the middle where the trader
    holds nothing, so it is only used where replace is unavailable.
    """
    from app.brokers.base import BrokerOrderRequest  # noqa: PLC0415

    req = BrokerOrderRequest(
        instrument_type=order.instrument_type,
        symbol=order.symbol,
        side=order.side,
        order_type=OrderType.LIMIT,
        quantity=order.quantity,
        limit_price=new_price,
        option_expiry=order.option_expiry,
        option_strike=order.option_strike,
        option_right=order.option_right,
        client_order_id=str(order.id),
    )
    # reprice_one() has already established the broker can replace atomically.
    # There is deliberately no cancel-then-place fallback: if the place failed
    # after the cancel succeeded, the entry would be gone with nothing in its
    # stead — the failure this whole guard exists to prevent.
    result = adapter.replace_order(order.broker_order_id, req)
    if getattr(result, "broker_order_id", None):
        order.broker_order_id = result.broker_order_id


def _tick() -> None:
    for oid in _due_order_ids():
        try:
            outcome = reprice_one(oid)
            if outcome not in ("gone", "already"):
                log.info("discord reprice: order %s -> %s", oid, outcome)
        except Exception:  # noqa: BLE001
            log.exception("discord reprice: failed on order %s", oid)


def poll_loop(shutdown_check=None) -> None:
    log.info(
        "discord_reprice: starting (interval=%ss, batch=%d)", POLL_INTERVAL_S, BATCH_SIZE
    )
    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("discord_reprice: shutdown requested, exiting")
            return
        try:
            _tick()
        except Exception:  # noqa: BLE001
            log.exception("discord_reprice: tick failed")
        time.sleep(POLL_INTERVAL_S)


__all__ = ["poll_loop", "reprice_one"]
