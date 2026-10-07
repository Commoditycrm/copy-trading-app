"""Rest each trim at the broker as a take-profit order.

The "Take-profit orders" exit mode. Instead of watching the price and selling
at market when a trim's Profit target is reached (auto-trim), the next trim
sits at the broker as a real LIMIT sell at that target — so it fills at the
target, and fills whether or not this app is up.

    entry fills        take-profit for Trim 1 rests (and the On Fill stop)
    Trim 1 fills       stop moves to Trim 1's level; take-profit for Trim 2 rests
    Trim 2 fills       stop moves to Trim 2's level; take-profit for Trim 3 …

── A stop and a take-profit on the same contracts ──────────────────────────────
A resting sell RESERVES the contracts it covers, so a take-profit and a stop
cannot simply both cover a position: the broker refuses the second. Two things
make it work:

* the take-profit covers only the SLICE the trim sells, and the plain ladder
  stop (discord_stop_orders) covers the rest — ``guard.tp_qty`` is the earmark
  that stop is sized around;
* the slice itself gets a stop LINKED to its take-profit — a broker pair where
  one filling cancels the other (BrokerAdapter.place_exit_pair; Webull options).
  So every contract held rests under a stop, including on the last trim, where
  the pair covers everything and there is no plain stop at all.

Both stops sit at the same level, so a break takes the whole position out at
the broker with nothing here involved.

Only on brokers with ``supports_exit_pair``. On the rest this mode runs as
auto-trim does (see discord_auto_trim) — without a linked pair the last trim
could rest either its stop or its take-profit, never both.

── Reconciled, like the stop ───────────────────────────────────────────────────
Every pass compares what SHOULD rest with what does and fixes the difference,
so it survives an add that changed the size, an average that moved the entry, a
stop level set by hand, or a restart. A take-profit the trader cancels is
honoured (``tp_off``); one the broker refuses is not retried for a while.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy.orm import Session

from app.services import discord_ladder, discord_position_guard as guards

log = logging.getLogger(__name__)

_CENT = Decimal("0.01")
# After a refused take-profit: long enough that a persistent cause produces a
# handful of orders a day, not one every pass.
BACKOFF = timedelta(minutes=15)
# …but a refusal because the contracts were still held by the order being
# replaced is retried as soon as that order has had time to go.
CONFLICT_BACKOFF = timedelta(seconds=45)


def enabled(ts) -> bool:
    """Is the Take-profit orders exit mode on under these settings? Manual wins."""
    return bool(
        ts is not None and getattr(ts, "discord_tp_orders", False)
        and not getattr(ts, "discord_manual_exit", False)
    )


def active(ts, adapter) -> bool:
    """On, AND this broker can link a take-profit to a stop. Where it cannot,
    the mode runs as auto-trim instead."""
    return enabled(ts) and bool(getattr(adapter, "supports_exit_pair", False))


def possible(db: Session, user_id, account_ts) -> bool:
    """Could ANY of this trader's holdings be in the mode — the account, or a
    channel with its own settings? One query, so the poller can skip the
    per-holding settings lookups for the many traders who never turn it on."""
    if enabled(account_ts):
        return True
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.discord_alert_source import DiscordAlertSource  # noqa: PLC0415

    for own in db.execute(
        select(DiscordAlertSource.channel_settings).where(
            DiscordAlertSource.user_id == user_id,
            DiscordAlertSource.use_account_settings.is_(False),
        )
    ).scalars():
        if isinstance(own, dict) and own.get("discord_tp_orders"):
            return True
    return False


def _from_an_earlier_day(order) -> bool:
    """Was this order placed on an earlier market day? A take-profit is a DAY
    order, so one from yesterday that is no longer working simply expired."""
    from app.services import market_hours  # noqa: PLC0415

    placed = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
    if placed is None:
        return False
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=timezone.utc)
    return placed.astimezone(market_hours.ET).date() < market_hours.now_et().date()


@dataclass(frozen=True)
class Plan:
    """The take-profit that should be resting for a holding."""

    rung: int
    quantity: Decimal
    price: Decimal
    # The stop linked to it, on the same contracts. None = no stop level is set
    # (or the market is already at the target), so the take-profit rests alone.
    stop_price: Decimal | None


def target_price(entry: Decimal, gate_pct: Decimal) -> Decimal:
    """The trim's Profit target as a price, rounded UP to the cent — never a
    fill below the gain that was asked for."""
    return (entry * (Decimal(1) + gate_pct / Decimal(100))).quantize(_CENT, rounding=ROUND_CEILING)


def plan(guard, held: Decimal, ts, mark: Decimal | None) -> Plan | None:
    """What should rest for the NEXT trim, or None. Pure — no DB, no broker."""
    if guard.tp_off or held <= 0 or guard.closed_at is not None:
        return None
    entry = guard.entry_price
    if entry is None or entry <= 0:
        return None
    cfg = discord_ladder.trim_config(ts)
    rung = (guard.sell_count or 0) + 1
    if rung > cfg.count:
        return None                       # ladder spent: what is left is a runner
    gate = cfg.rung(rung).profit_gate_pct or Decimal(0)
    if gate <= 0:
        return None                       # no target: this trim waits for its alert
    quantity, _runner = guards.rung_quantity(cfg, rung, held)
    if quantity <= 0:
        return None                       # a last trim that rounds down to nothing
    price = target_price(Decimal(str(entry)), gate)
    stop = guard.stop_price
    if stop is not None and stop <= 0:
        stop = None
    if stop is not None and mark is not None and mark <= stop:
        # The market is at or through the stop. A stop there would be refused,
        # and the ladder stop is about to take the position out anyway.
        return None
    if mark is not None and mark >= price:
        # Already at the target: a linked take-profit must sit ABOVE the market,
        # so it goes out as a plain limit, which fills at once.
        stop = None
    return Plan(rung=rung, quantity=quantity, price=price, stop_price=stop)


# ── what is resting ─────────────────────────────────────────────────────────

def _working(order) -> bool:
    from app.models.order import OrderStatus  # noqa: PLC0415

    return order is not None and order.status in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
    )


def _clear(guard) -> None:
    guard.tp_order_id = None
    guard.tp_stop_order_id = None
    guard.tp_rung = None
    guard.tp_qty = None


def release(db: Session, guard, cancel) -> bool:
    """Cancel the resting take-profit (and its linked stop) so the contracts are
    free — before a trim, a close or a stop-out places its own exit. Not a
    trader's cancel: the next pass puts back whatever should rest."""
    from app.models.order import Order  # noqa: PLC0415

    done = False
    for oid in (guard.tp_order_id, guard.tp_stop_order_id):
        if oid is None:
            continue
        if _working(db.get(Order, oid)):
            try:
                cancel(oid)
                done = True
            except Exception:  # noqa: BLE001
                log.warning("take-profit: could not release %s", guard.symbol, exc_info=True)
    _clear(guard)
    if done:
        log.info("take-profit: released %s's resting orders to make room for an exit", guard.symbol)
    return done


def settle(db: Session, guard, ts, cancel, *, in_session: bool = True) -> str | None:
    """Catch up with what happened to the resting orders since the last pass.

    Returns a short outcome when something changed, else None.
    """
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    if guard.tp_order_id is None and guard.tp_stop_order_id is None:
        return None
    tp = db.get(Order, guard.tp_order_id) if guard.tp_order_id else None
    sl = db.get(Order, guard.tp_stop_order_id) if guard.tp_stop_order_id else None

    # ── the take-profit filled: the trim happened ───────────────────────────
    if tp is not None and tp.status == OrderStatus.FILLED:
        rung = guard.tp_rung or ((guard.sell_count or 0) + 1)
        cfg = discord_ladder.trim_config(ts)
        guard.sell_count = max(guard.sell_count or 0, rung)
        # A trailing stop starts its give-back below the price the trim sold at.
        stop, trail_pct, peak = guards.rung_stop(
            guard.entry_price, cfg.rung(rung), tp.filled_avg_price or tp.limit_price)
        stop = guards._to_tick(stop)
        if stop is not None and stop > 0:
            from app.services.position_events import because  # noqa: PLC0415

            with because(f"Trim {rung} take-profit filled — the ladder's stop for what is left"):
                guard.stop_price = stop           # this trim's stop, on whatever is left
                guard.stop_trail_pct, guard.stop_peak = trail_pct, peak
                if hasattr(db, "flush"):
                    db.flush()
        # The broker cancels the linked stop itself; make sure, and keep our row honest.
        if _working(sl):
            try:
                cancel(sl.id)
            except Exception:  # noqa: BLE001
                log.warning("take-profit: linked stop cancel failed for %s", guard.symbol, exc_info=True)
        _clear(guard)
        log.info("take-profit: %s trim %s filled %s @ %s — stop now %s",
                 guard.symbol, rung, tp.filled_quantity, tp.filled_avg_price, guard.stop_price)
        return f"trim {rung} filled"

    # ── the linked stop filled: that slice was stopped out ──────────────────
    if sl is not None and sl.status == OrderStatus.FILLED:
        if _working(tp):
            try:
                cancel(tp.id)
            except Exception:  # noqa: BLE001
                log.warning("take-profit: cancel after stop-out failed for %s", guard.symbol, exc_info=True)
        _clear(guard)
        return "stopped out"

    if tp is None or _working(tp):
        if tp is not None and sl is not None and not _working(sl):
            # The stop leg is gone (cancelled with the row's other stops) while
            # the take-profit still rests: it carries on alone.
            guard.tp_stop_order_id = None
            return "linked stop removed"
        if tp is None:
            _clear(guard)
            return "cleared"
        return None

    # ── the take-profit is no longer working, and did not fill ──────────────
    if _working(sl):
        try:
            cancel(sl.id)
        except Exception:  # noqa: BLE001
            log.warning("take-profit: orphan stop cancel failed for %s", guard.symbol, exc_info=True)
    status = tp.status
    expired = _from_an_earlier_day(tp)
    _clear(guard)
    if status == OrderStatus.CANCELED and in_session and not expired:
        # Every cancel WE make clears these ids first (release / replace), so a
        # take-profit the guard still pointed at that is now cancelled was
        # cancelled by the trader. Honour it, as the ladder stop does.
        guard.tp_off = True
        log.warning("take-profit: %s's order was cancelled outside the ladder — not re-placing", guard.symbol)
        return "removed by the trader"
    if status == OrderStatus.REJECTED:
        guard.tp_backoff_until = datetime.now(timezone.utc) + BACKOFF
        return "rejected — backing off"
    # Expired — a DAY order at the close. It can read as a cancel, so one seen
    # outside the session, or left from an earlier day, counts as an expiry: it
    # is placed again next session.
    return "expired"


def in_sync(db: Session, guard, want: Plan | None) -> bool:
    """Does what rests match ``want``?"""
    from app.models.order import Order  # noqa: PLC0415

    tp = db.get(Order, guard.tp_order_id) if guard.tp_order_id else None
    if want is None:
        return tp is None
    if not _working(tp) or guard.tp_rung != want.rung:
        return False
    if Decimal(str(tp.quantity)) != want.quantity or tp.limit_price is None \
            or Decimal(str(tp.limit_price)) != want.price:
        return False
    sl = db.get(Order, guard.tp_stop_order_id) if guard.tp_stop_order_id else None
    if want.stop_price is None:
        return True                       # a lone take-profit is never torn down to drop a stop
    if not _working(sl):
        # No stop linked. Fine if it was removed by hand (the level went with
        # it); otherwise a stop level now exists that this pair should carry.
        return False
    return sl.stop_price is not None and Decimal(str(sl.stop_price)) == want.stop_price


def reconcile(db: Session, guard, held: Decimal, ts, mark: Decimal | None, *,
              place_limit, place_pair, cancel, reconcile_stop,
              in_session: bool = True, now: datetime | None = None) -> str:
    """Make the broker match the ladder for one holding. Returns an outcome.

    Injected, so this never talks to a broker itself:
      ``place_limit(quantity, price) -> order_id``
      ``place_pair(quantity, price, stop_price) -> (tp_order_id, stop_order_id)``
      ``cancel(order_id)``
      ``reconcile_stop() -> str``   the plain ladder stop's own reconcile

    The order matters. The earmark (``tp_qty``) is set first, then the ladder
    stop is reconciled — shrinking it to what the take-profit leaves — and only
    then is the take-profit placed, so the two never ask for the same contracts.
    """
    now = now or datetime.now(timezone.utc)
    settled = settle(db, guard, ts, cancel, in_session=in_session)

    want = plan(guard, held, ts, mark)
    released = False
    if not in_sync(db, guard, want):
        released = release(db, guard, cancel)
    resting = guard.tp_order_id is not None

    # Nothing new goes out while the market is closed (a DAY order would only
    # expire) or while backing off after a refusal — but what rests stays.
    backing_off = guard.tp_backoff_until is not None and guard.tp_backoff_until > now
    # Not in the pass that cancelled the old one: the broker frees its
    # contracts only once the cancel completes, and a new sell placed in the
    # same instant is refused as opening a naked call (QA 2026-10-06).
    will_place = (want is not None and not resting and in_session
                  and not backing_off and not released)

    # Earmark only contracts a take-profit actually holds, or is about to —
    # including the one going out next pass, so the stop is sized around it now.
    guard.tp_qty = (want.quantity if (want is not None and (resting or will_place or released))
                    else None)
    stop_outcome = reconcile_stop()
    if want is None:
        return settled or f"no take-profit (stop: {stop_outcome})"
    if resting:
        return settled or f"in sync (stop: {stop_outcome})"
    if released and want is not None and not resting:
        return settled or "replacing — the new take-profit goes out next pass"
    if not will_place:
        return settled or ("market closed" if not in_session else "backing off")
    if stop_outcome.startswith("closed") or stop_outcome == "exit sent":
        guard.tp_qty = None
        return f"position being closed (stop: {stop_outcome})"

    from app.services.position_events import because  # noqa: PLC0415

    try:
        with because(f"take-profit for Trim {want.rung} at {want.price}"
                     + (f", linked stop {want.stop_price}" if want.stop_price is not None else "")):
            if want.stop_price is not None:
                tp_id, sl_id = place_pair(want.quantity, want.price, want.stop_price)
            else:
                tp_id, sl_id = place_limit(want.quantity, want.price), None
    except Exception as exc:  # noqa: BLE001
        # Nothing rests, so nothing is earmarked: the ladder stop grows back to
        # the whole position on the next pass.
        guard.tp_qty = None
        from app.services.discord_stop_orders import is_reservation_conflict  # noqa: PLC0415

        guard.tp_backoff_until = now + (
            CONFLICT_BACKOFF if is_reservation_conflict(str(exc)) else BACKOFF)
        log.warning("take-profit: %s trim %s refused (%s) — retrying after %s",
                    guard.symbol, want.rung, str(exc)[:200], guard.tp_backoff_until)
        return "refused — backing off"
    guard.tp_order_id, guard.tp_stop_order_id, guard.tp_rung = tp_id, sl_id, want.rung
    log.info("take-profit: resting SELL %s %s @ %s for trim %s%s",
             want.quantity, guard.symbol, want.price, want.rung,
             f", linked stop @ {want.stop_price}" if sl_id else "")
    return (f"{settled}; " if settled else "") + f"placed trim {want.rung}: {want.quantity} @ {want.price}"


__all__ = ["CONFLICT_BACKOFF", "enabled", "active", "possible", "Plan", "plan", "target_price", "settle", "release",
           "in_sync", "reconcile", "BACKOFF"]
