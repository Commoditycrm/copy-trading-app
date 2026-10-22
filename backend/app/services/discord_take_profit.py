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
from dataclasses import dataclass, field
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
    # How ``quantity`` was reached, for the order's "Why" line only: the rung's
    # sell % and the contracts held. Not part of what rests, so not compared.
    qty_pct: Decimal | None = field(default=None, compare=False)
    held: Decimal | None = field(default=None, compare=False)


def _n(d: Decimal) -> str:
    """2.000000 -> "2", 1.40 -> "1.4"."""
    d = Decimal(str(d)).normalize()
    return format(d.quantize(Decimal(1)) if d == d.to_integral_value() else d, "f")


def _px(d: Decimal) -> str:
    """A price as the trader reads it: 0.3600 -> "0.36", 1.2 -> "1.20",
    0.125 -> "0.125"."""
    d = Decimal(str(d))
    cents = d.quantize(_CENT)
    return format(cents if cents == d else d.normalize(), "f")


def sizing_note(p: Plan) -> str:
    """ "sell 70% of 2 = 1.4 → rounded up to 2 ctr" — the trim's size and how it
    was reached. Every trim rounds UP (guards.rung_quantity), and the trader
    should see that in the order's Why, not work it out."""
    unit = "ctr"
    if p.qty_pct is None or p.held is None:
        return f"sell {_n(p.quantity)} {unit}"
    exact = Decimal(str(p.held)) * Decimal(str(p.qty_pct)) / Decimal(100)
    head = f"sell {_n(p.qty_pct)}% of {_n(p.held)}"
    if exact == p.quantity:
        return f"{head} = {_n(p.quantity)} {unit}"
    return f"{head} = {_n(exact)} → rounded up to {_n(p.quantity)} {unit}"


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
    return Plan(rung=rung, quantity=quantity, price=price, stop_price=stop,
                qty_pct=Decimal(str(cfg.rung(rung).qty_pct)), held=held)


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
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

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
    # The cancel can find that an order had already FILLED (the canceller
    # records that — exit_confirm.cancel_and_record). Then the trim happened:
    # keep the ids so settle() credits it — the rung, the stop for what is left —
    # instead of forgetting it and selling the same trim again.
    for oid in (guard.tp_order_id, guard.tp_stop_order_id):
        o = db.get(Order, oid) if oid else None
        if o is not None and o.status == OrderStatus.FILLED:
            log.info("take-profit: %s's order filled before it could be released — "
                     "crediting the trim", guard.symbol)
            return True
    # …or that the broker could not say what happened (a rate limit, most
    # often), and the row is still working. Forgetting it then is how a fill was
    # lost: QA 2026-10-19, QQQ 722P — Trim 1 sold 8 @ 1.22, was recorded as
    # cancelled, and Trim 1 then sold the 2 left for Trim 2. Keep the ids: the
    # next pass asks again.
    for oid in (guard.tp_order_id, guard.tp_stop_order_id):
        if oid is not None and _working(db.get(Order, oid)):
            log.warning("take-profit: %s's cancel is not confirmed — keeping the order "
                        "and asking the broker again next pass", guard.symbol)
            return True
    _clear(guard)
    if done:
        log.info("take-profit: released %s's resting orders to make room for an exit", guard.symbol)
    return done


def settle(db: Session, guard, ts, cancel, *, in_session: bool = True,
           held: Decimal | None = None) -> str | None:
    """Catch up with what happened to the resting orders since the last pass.

    ``held`` is the broker's position this tick, when the caller has it. A trim
    that sold everything leaves nothing for a stop to protect, so the ladder's
    stop is not moved.

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
        # The trim sold the whole position: there is nothing left to stop. Moving
        # the stop anyway logged "Trailing stop raised 0.36 → 2.02" on a flat
        # position, a second before the ladder retired (QA 2026-10-15, QQQ 718P).
        flat = held is not None and held <= 0
        if flat:
            log.info("take-profit: %s trim %s sold the whole position — no stop to move",
                     guard.symbol, rung)
        if stop is not None and stop > 0 and not flat:
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


# A take-profit for more contracts than are held can only have (partly)
# filled — it reserves what it covers, so nothing else could have sold them.
# While the broker still calls it working, wait this long for it to say filled
# before replacing it anyway.
LOOKS_FILLED_WAIT = timedelta(minutes=2)


def _age(order, now: datetime) -> timedelta | None:
    placed = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
    if placed is None:
        return None
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=timezone.utc)
    return now - placed


def _refresh_resting(db: Session, guard, refresh) -> bool:
    """Ask the broker what became of the resting take-profit, its linked stop
    and the plain ladder stop, onto their rows. False when one could not be read."""
    from app.models.order import Order  # noqa: PLC0415

    ok = True
    for oid in (guard.tp_order_id, guard.tp_stop_order_id, getattr(guard, "stop_order_id", None)):
        if oid is not None and _working(db.get(Order, oid)):
            ok = refresh(oid) and ok
    return ok


# How often a holding that holds FEWER contracts than its resting orders cover
# asks the broker about them, while the answer keeps coming back "working".
CHECK_EVERY = timedelta(seconds=15)
_last_check: dict = {}


def _sold_unseen(db: Session, guard, held: Decimal, now: datetime) -> bool:
    """Fewer contracts held than our resting sells cover — the take-profit's
    slice plus the plain stop on the rest — so one of them has sold without
    our rows hearing of it. Each resting sell reserves its contracts, so
    nothing else could have taken them.

    QA 2026-10-21, QQQ 726C (Srini): Trim 1 (1 of 2) filled at 14:02:47 and
    Webull then held 1. Re-planned, Trim 1 was "50% of 1 = 1 @ 1.13" — the very
    order that had filled — so nothing was out of sync and nobody asked. The
    stop stayed at 0.22 instead of moving to break-even, Trim 2 never rested,
    and the trader sold the last contract by hand in the 10:04 selloff.
    """
    from app.models.order import Order  # noqa: PLC0415

    tp = db.get(Order, guard.tp_order_id) if guard.tp_order_id else None
    if not _working(tp):
        return False
    covered = Decimal(str(tp.quantity))
    stop_id = getattr(guard, "stop_order_id", None)
    stop = db.get(Order, stop_id) if stop_id else None
    if _working(stop):
        covered += Decimal(str(stop.quantity))
    if held >= covered:
        return False
    key = getattr(guard, "id", None) or id(guard)
    last = _last_check.get(key)
    if last is not None and now - last < CHECK_EVERY:
        return False
    _last_check[key] = now
    if len(_last_check) > 1000:
        _last_check.clear()
    return True


def _why_wait(db: Session, guard, held: Decimal, now: datetime) -> str | None:
    """A reason not to cancel the resting take-profit yet, or None."""
    from app.models.order import Order  # noqa: PLC0415

    tp = db.get(Order, guard.tp_order_id) if guard.tp_order_id else None
    if not _working(tp):
        return None
    qty = Decimal(str(tp.quantity))
    age = _age(tp, now)
    if held < qty and age is not None and age < LOOKS_FILLED_WAIT:
        return (f"{_n(held)} held is fewer than its {_n(qty)}, so it has filled — "
                "waiting for the broker to report it")
    return None


def reconcile(db: Session, guard, held: Decimal, ts, mark: Decimal | None, *,
              place_limit, place_pair, cancel, reconcile_stop,
              refresh=None, in_session: bool = True, now: datetime | None = None) -> str:
    """Make the broker match the ladder for one holding. Returns an outcome.

    Injected, so this never talks to a broker itself:
      ``place_limit(quantity, price) -> order_id``
      ``place_pair(quantity, price, stop_price) -> (tp_order_id, stop_order_id)``
      ``cancel(order_id)``
      ``reconcile_stop() -> str``   the plain ladder stop's own reconcile
      ``refresh(order_id) -> bool`` read one order from the broker onto its row;
                                    False when the broker could not be read

    Nothing that rests is replaced on the word of our own rows alone. A
    take-profit can fill with nothing here hearing of it (a Discord
    subscriber's Webull account has no order listener), and the position then
    no longer matches the plan — which reads exactly like a resize. So before a
    resting take-profit is cancelled the broker is asked what became of it, and
    while it cannot say, or says "working" for more contracts than are held,
    everything is left as it is for the pass.

    The order matters. The earmark (``tp_qty``) is set first, then the ladder
    stop is reconciled — shrinking it to what the take-profit leaves — and only
    then is the take-profit placed, so the two never ask for the same contracts.
    """
    now = now or datetime.now(timezone.utc)
    settled = settle(db, guard, ts, cancel, in_session=in_session, held=held)

    want = plan(guard, held, ts, mark)
    released = False
    if (refresh is not None and guard.tp_order_id is not None
            and (not in_sync(db, guard, want) or _sold_unseen(db, guard, held, now))):
        readable = _refresh_resting(db, guard, refresh)
        settled = settle(db, guard, ts, cancel, in_session=in_session, held=held) or settled
        want = plan(guard, held, ts, mark)
        if not in_sync(db, guard, want):
            why = (_why_wait(db, guard, held, now) if readable
                   else "the broker could not be read — asking again next pass")
            if why is not None:
                # Neither leg is touched, and nor is the stop: sizing it around
                # an earmark that may already have sold would pull the stop
                # off what is left.
                log.info("take-profit: %s — %s", guard.symbol, why)
                return (f"{settled}; " if settled else "") + f"take-profit kept: {why}"
    if not in_sync(db, guard, want):
        released = release(db, guard, cancel)
        if guard.tp_order_id is not None or guard.tp_stop_order_id is not None:
            # The release found the take-profit (or its linked stop) already
            # filled — usually WHY it was out of sync: the fill shrank the
            # position, so the plan no longer matched. Credit it now and plan the
            # next trim from what is left. The linked leg's cancel is still
            # completing, so the next order waits a pass (released stays True).
            settled = settle(db, guard, ts, cancel, in_session=in_session, held=held) or settled
            want = plan(guard, held, ts, mark)
    resting = guard.tp_order_id is not None
    # A cancel the broker could not confirm leaves the old take-profit resting.
    unconfirmed = released and resting

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
    # What rests is what is earmarked — the order at the broker, which is the
    # plan's size except while a cancel is unconfirmed.
    if resting:
        from app.models.order import Order  # noqa: PLC0415

        row = db.get(Order, guard.tp_order_id)
        guard.tp_qty = (Decimal(str(row.quantity)) if row is not None
                        else (want.quantity if want is not None else None))
    else:
        guard.tp_qty = (want.quantity if (want is not None and (will_place or released))
                        else None)
    stop_outcome = reconcile_stop()
    if unconfirmed:
        return settled or f"cancel not confirmed — asking again next pass (stop: {stop_outcome})"
    if want is None:
        return settled or f"no take-profit (stop: {stop_outcome})"
    if resting:
        return settled or f"in sync (stop: {stop_outcome})"
    if released and want is not None and not resting:
        return settled or "replacing — the new take-profit goes out next pass"
    if not will_place:
        return settled or ("market closed" if not in_session else "backing off")
    if (stop_outcome.startswith("closed") or stop_outcome == "exit sent"
            or stop_outcome in ("waiting (exit working)", "exit already working")):
        # An exit is selling these contracts (a trim, a manual close, a refused
        # stop's exit): a take-profit placed now would reserve them and the
        # broker would refuse that exit. The stop waits for it; so does this.
        guard.tp_qty = None
        return f"position being closed (stop: {stop_outcome})"

    # The ladder stop was just CANCELLED so it could be resized around this
    # take-profit, and its replacement goes out next pass. Those contracts are
    # not free yet — a broker releases them only once the cancel completes — so
    # a take-profit placed now asks for contracts the old stop still holds and
    # is refused as opening a naked position. Live 2026-10-13: an entry filled
    # 5 @ 1.08, the On Fill stop took all 5, and the Trim 1 take-profit for 3
    # was refused 32 seconds later.
    #
    # Wait one pass, exactly as the take-profit's own replace does above. The
    # earmark (tp_qty) stays, so the stop that goes out next pass is already
    # sized around this take-profit and the two never ask for the same
    # contracts. No backoff: nothing was refused.
    from app.services import discord_stop_orders as _stops  # noqa: PLC0415

    if _stops.is_replacing(stop_outcome):
        return (f"{settled}; " if settled else "") + (
            "take-profit waits a pass — the stop is being resized around it")

    from app.services.position_events import because  # noqa: PLC0415

    try:
        with because(f"Trim {want.rung}: {sizing_note(want)} at {_px(want.price)}"
                     + (f", linked stop {_px(want.stop_price)}" if want.stop_price is not None else "")):
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


__all__ = ["CHECK_EVERY", "CONFLICT_BACKOFF", "LOOKS_FILLED_WAIT", "enabled", "active", "possible", "Plan", "plan", "target_price", "settle", "release",
           "in_sync", "reconcile", "BACKOFF"]
