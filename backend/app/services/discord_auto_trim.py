"""Fire the exit ladder off the PRICE instead of waiting for an alert.

    1st Trim, Min Profit 20%, Auto Trim ON
        position reaches +20%  ->  sell half, stop the rest below entry
                                   (no Discord alert involved)

── The same rung, by a different trigger ───────────────────────────────────
Nothing here decides what a trim DOES. It works out which rung is next and
whether its gate has been reached, then submits the exit through the ordinary
alert pipeline — the Self channel's own endpoint. So the sizing, the stop
placement, the trailing-exit branch, the subscriber fanout, the Channel column
and the audit trail are not re-implemented, they are literally the same code.
A second trim path would be a second thing to keep correct, and it would be the
one nobody tests.

── A gate of 0 is never auto-fired ─────────────────────────────────────────
Zero means "no minimum". That is the right default for an ALERT-driven rung —
sell whenever the author says to — and meaningless without an alert, because
"reached 0% profit" is true the instant a position is up a cent. Auto-firing
those would walk rungs 2 and 3 immediately after rung 1 and flatten the
position on the same tick. So a rung needs a positive threshold to be
automatic, which is also the only way a trader can say WHERE they want each
automatic trim to happen.

── Once per rung, per position ─────────────────────────────────────────────
The guard's sell_count is the rung counter and plan_exit advances it, so a
fired rung cannot fire again while the price stays above its gate. What stops a
DOUBLE fire inside one tick is that this runs sequentially per guard and the
count is committed before the next guard is considered.

Across ticks it is a per-trader Redis lock. Two processes sweep: the worker's
poll loop, and the web tier right after a Simulated Prices pin. Without the
lock both read the same sell_count, both see the rung due, and both sell it.
Each guard is refreshed once the lock is held, so the second sweep sees the
count the first one committed.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from decimal import Decimal

from app.database import SessionLocal
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

log = logging.getLogger(__name__)

# One sweep every 15s, the same for every broker.
#
# Each sweep costs one get_positions per account, and that budget is shared:
# Webull allows 10 requests per 30 SECONDS across the pnl poller, the order
# listener and this, which is how the 429 storm happened that the
# once-per-account position fetch was written to fix. Alpaca is far roomier
# (200/min), but a single cadence is worth more than the few seconds a
# broker-aware one would save — the detection lag is bounded by this, while the
# stop now goes on within ~1s of the fill regardless (see pnl_poller.poll_now).
POLL_INTERVAL_S = 15

# A sweep for one trader can include several trims, each with its own fanout, so
# the lock outlives a slow sweep; the TTL only matters if a process dies holding
# it. A pin waits briefly for a worker sweep in progress rather than skipping.
_LOCK_TTL_S = 120
_LOCK_WAIT_S = 10


def _enabled(ts) -> bool:
    return bool(ts is not None and getattr(ts, "discord_auto_trim", False))


def _engine(ts) -> str:
    """Which exit engine this trader runs: "ladder" (default) or "ai"."""
    return "ai" if getattr(ts, "discord_exit_engine", None) == "ai" else "ladder"


def _gate_for(ts, rung: int) -> Decimal:
    """This rung's Min Profit to Trim, as configured. 0 means "no minimum"."""
    field = {
        1: "discord_trim_profit_gate_pct",
        2: "discord_trim2_profit_gate_pct",
        3: "discord_trim3_profit_gate_pct",
    }.get(rung, "discord_trim3_profit_gate_pct")
    raw = getattr(ts, field, None)
    try:
        return Decimal(str(raw)) if raw is not None else Decimal(0)
    except Exception:  # noqa: BLE001
        return Decimal(0)


def gain_pct(entry: Decimal | None, mark: Decimal | None) -> Decimal | None:
    """Profit over the ladder's entry reference, in percent.

    Measured against ``entry_price`` — the same number every rung's gate and
    stop key off — so an auto-trim fires at exactly the level the trader
    configured, and averaging down moves it with the real cost basis.
    """
    if entry is None or mark is None:
        return None
    entry, mark = Decimal(str(entry)), Decimal(str(mark))
    if entry <= 0 or mark <= 0:
        return None
    return (mark - entry) / entry * Decimal(100)


def due_rung(ts, guard: DiscordPositionGuard, mark: Decimal | None) -> int | None:
    """The rung to fire now, or None. Pure — no DB, no broker."""
    if not _enabled(ts):
        return None
    rung = (guard.sell_count or 0) + 1
    if rung > 3:
        # The ladder has three steps. Past the third there is nothing left to
        # automate: the final rung exits what remains.
        return None
    gate = _gate_for(ts, rung)
    if gate <= 0:
        return None
    gain = gain_pct(guard.entry_price, mark)
    if gain is None:
        return None
    # Inclusive, exactly as the alert path's gate is: "up 20%" trims AT a 20%
    # threshold, not only past it.
    return rung if gain >= gate else None


def _strike_text(v) -> str:
    """The strike, written the way a human writes it.

    Decimal.normalize() renders a round number in SCIENTIFIC notation —
    Decimal("230").normalize() is 2.3E+2 — so the alert came out as
    "✂️ $NVDA 2.3E+2C 09/28", which no parser recognises. Live 2026-09-28 that
    silently disabled auto-trim for every strike ending in a zero: the message
    was stored as "not a trade alert" and the rung never fired. NVDA only
    looked healthy because real Discord alerts were driving it.

    format(..., "f") never uses an exponent; the trailing-zero strip keeps
    342.50 reading as 342.5.
    """
    s = format(Decimal(str(v or 0)), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _exit_alert_text(guard: DiscordPositionGuard) -> str:
    """The synthetic alert that fires this rung.

    Spells the contract out in the compact format the parser already reads, and
    marks it with the scissors that make it an EXIT. Writing it as text rather
    than calling execution directly is what keeps this on the one trim path —
    it is read by the same parser that reads the author's own trims.
    """
    right = guard.option_right
    cp = "C" if (right == OptionRight.CALL or right == "call") else "P"
    return (f"✂️ ${guard.symbol} {_strike_text(guard.option_strike)}{cp} "
            f"{guard.option_expiry:%m/%d}")


def _live_guards(db, user_id=None):
    from sqlalchemy import select  # noqa: PLC0415

    statement = select(DiscordPositionGuard).where(
        DiscordPositionGuard.closed_at.is_(None),
        DiscordPositionGuard.entry_price.is_not(None),
        # Options only: the alert text this builds names a contract, and
        # the ladder has never run on anything else.
        DiscordPositionGuard.option_strike.is_not(None),
    )
    if user_id is not None:
        statement = statement.where(DiscordPositionGuard.user_id == user_id)
    return list(db.execute(statement).scalars())


def _mark_for(positions, guard, user_id=None) -> Decimal | None:
    """The broker's own mark for this contract, from the position it holds.

    Takes the account's ALREADY-FETCHED positions (fetched once per account per
    sweep by the caller) — reading them per-guard blew Webull's 10-req/30s limit
    (429 storm). Read from the POSITION rather than a quote endpoint because
    Alpaca exposes no option quote, and because a mark with no position behind it
    would fire a trim on something already gone.
    """
    for p in positions:
        if (p.symbol or "").upper() != guard.symbol:
            continue
        if p.option_strike != guard.option_strike:
            continue
        if p.option_right != guard.option_right:
            continue
        if p.option_expiry != guard.option_expiry:
            continue
        if (p.quantity or 0) == 0:
            return None
        # Test-only pins from Simulated Prices must exercise the automatic
        # trim ladder too, not just stop/trailing enforcement. Pins are scoped
        # by trader and contract and are disabled outside a test-enabled env.
        if user_id is not None:
            from app.services import price_override  # noqa: PLC0415
            pinned = price_override.apply_to(user_id, p)
            if pinned is not None:
                return pinned
        return _dec(getattr(p, "current_price", None))
    return None


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d > 0 else None


@contextmanager
def _trader_lock(trader_id, *, required: bool):
    """Hold this trader's sweep lock; yields whether the sweep may proceed.

    With Redis down, the worker (``required=False``) sweeps unlocked, as it did
    before the lock existed, so auto-trim keeps working. A pin-triggered sweep
    (``required=True``) skips instead: it is the second sweeper, and the worker
    picks the move up on its next tick anyway.
    """
    lock = None
    try:
        from app.services.redis_client import get_sync_redis  # noqa: PLC0415
        lock = get_sync_redis().lock(
            f"discord:autotrim:lock:{trader_id}",
            timeout=_LOCK_TTL_S,
            blocking_timeout=_LOCK_WAIT_S,
        )
        acquired = bool(lock.acquire())
        if not acquired:
            log.info("auto-trim: trader %s is already being swept — skipping", trader_id)
    except Exception:  # noqa: BLE001
        log.warning("auto-trim: sweep lock unavailable for %s", trader_id, exc_info=True)
        lock = None
        acquired = not required
    try:
        yield acquired
    finally:
        if lock is not None and acquired:
            try:
                lock.release()
            except Exception:  # noqa: BLE001
                # Expired mid-sweep (TTL) — nothing left to release.
                log.warning("auto-trim: sweep lock for %s expired before release", trader_id)


def tick(user_id=None) -> None:
    """One sweep, optionally scoped to one trader.

    The normal worker leaves ``user_id`` empty. Simulated Prices supplies the
    trader id immediately after changing a test price so each one-second path
    step is evaluated then, rather than waiting for the worker's next sweep.
    """
    with SessionLocal() as db:
        guards = _live_guards(db, user_id)
        if not guards:
            return
        by_user: dict = {}
        for g in guards:
            by_user.setdefault(g.user_id, []).append(g)

        for trader_id, rows in by_user.items():
            # Only the pin-triggered sweep insists on the lock; see _trader_lock.
            with _trader_lock(trader_id, required=user_id is not None) as ok:
                if ok:
                    _sweep_trader(db, trader_id, rows)


def _sweep_trader(db, trader_id, rows) -> None:
    """Fire whatever rungs are due for one trader. Caller holds the sweep lock."""
    from app.api.discord_sources import submit_self_alert_text  # noqa: PLC0415
    from app.brokers import adapter_for  # noqa: PLC0415
    # `guards` is a local in tick, so the module is aliased.
    from app.services import discord_position_guard as pg  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.models.settings import TraderSettings  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    ts = db.get(TraderSettings, trader_id)
    # The AI engine runs whether or not ladder auto-trim is on; with it selected
    # the ladder's rungs are never auto-fired, so two engines can't both sell.
    # The engine is account-wide; the LADDER (gates, auto-trim on/off) is per
    # position — the settings of the channel that opened it.
    engine = _engine(ts)
    from app.services import discord_channel_settings as dcs  # noqa: PLC0415

    guard_ts = {g.id: (dcs.for_guard(db, trader_id, g) or ts) for g in rows}
    # Nothing has auto-trim on: skip before the broker read (rate limits).
    if engine == "ladder" and not any(_enabled(t) for t in guard_ts.values()):
        return
    user = db.get(User, trader_id)
    if user is None:
        return
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == trader_id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return
    try:
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    except Exception:  # noqa: BLE001
        log.warning("auto-trim: no adapter for user %s", trader_id, exc_info=True)
        return

    # Fetch the account's positions ONCE per sweep and match every guard
    # against that list. Reading them per-guard made N broker calls per
    # sweep and blew Webull's 10-req/30s limit (429 storm on prod).
    try:
        # A TRIGGER check, not an order: a trim it fires re-reads live positions
        # before placing anything. So it may share a read another loop made
        # seconds ago (Webull's limits — see webull.py, shared snapshot).
        try:
            positions = adapter.get_positions(cached_ok=True)
        except TypeError:                 # brokers without the cached read
            positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        log.warning("auto-trim: could not read positions for user %s", trader_id, exc_info=True)
        return

    if engine == "ai":
        from app.services import ai_trim  # noqa: PLC0415

        live = []
        for guard in rows:
            db.refresh(guard)
            if guard.closed_at is None:
                live.append(guard)
        ai_trim.sweep(
            db, user, ts, acct, adapter, live, positions,
            lambda ps, g: _mark_for(ps, g, trader_id),
        )
        return

    for guard in rows:
        try:
            # Loaded before the lock was taken — another sweep may have fired a
            # rung or closed the guard since. Judge the committed state.
            db.refresh(guard)
            if guard.closed_at is not None:
                continue

            # Measure against the ACTUAL fill, not the limit we bid.
            #
            # A guard is seeded at placement with the limit, and only
            # the EXIT path used to replace it with the fill — so
            # auto-trim measured a price nobody paid. Live 2026-09-28:
            # QQQ was bid 0.65, repriced to 0.72, filled at 0.6875;
            # auto-trim read +7.7% off 0.65 and fired, the ladder
            # re-synced and answered "up 1.82%, under the 5% gate". The
            # rung was spent for nothing and the next sweep walked the
            # ladder down a position that never reached a target. SPY
            # went the same way and ended flat on a break-even stop that
            # only moved because of this.
            #
            # Idempotent (returns False when unchanged) and the exit
            # path still syncs too, so nothing else changes behaviour —
            # the reference is simply correct sooner.
            if pg.sync_entry_price(db, guard):
                db.commit()

            mark = _mark_for(positions, guard, trader_id)
            rung = due_rung(guard_ts.get(guard.id, ts), guard, mark)
            if rung is None:
                continue

            before_rung = guard.sell_count or 0
            before_trail = guard.trail_qty

            text = _exit_alert_text(guard)
            log.info(
                "auto-trim: %s reached %.2f%% — firing trim %s via %r",
                guard.symbol, gain_pct(guard.entry_price, mark), rung, text,
            )
            msg = submit_self_alert_text(db, user, text, approve=True)

            # If the rung sold nothing and armed nothing, give it back.
            #
            # plan_exit advances the rung unconditionally, and for a
            # HUMAN alert that is right: the trader's Nth alert is their
            # Nth trim whatever it managed to do. Auto-trim has no alert
            # — nobody asked for anything — so a rung that turns out not
            # to be due must not be spent, or the next sweep measures
            # the rung after it and the ladder walks itself out of a
            # position on a price wobble between check and execution.
            #
            # Judged on what actually happened rather than on the note:
            # an order id means it sold, a changed trail_qty means this
            # rung armed one. Any stop the rung set is deliberately
            # KEPT — the gate decides whether to sell, never whether to
            # protect.
            db.refresh(guard)
            did_something = (
                (msg is not None and msg.order_id is not None)
                or guard.trail_qty != before_trail
            )
            if not did_something and (guard.sell_count or 0) > before_rung:
                log.info(
                    "auto-trim: %s trim %s sold nothing — returning the rung",
                    guard.symbol, rung,
                )
                pg.rollback_exit(guard)
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("auto-trim: failed on %s", guard.symbol)


def poll_loop(shutdown_check=None) -> None:
    log.info("discord_auto_trim: starting (interval=%ss)", POLL_INTERVAL_S)
    while True:
        if shutdown_check is not None and shutdown_check():
            return
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("discord_auto_trim: tick failed")
        time.sleep(POLL_INTERVAL_S)
