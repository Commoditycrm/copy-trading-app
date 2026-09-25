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
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from app.database import SessionLocal
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 15


def _enabled(ts) -> bool:
    return bool(ts is not None and getattr(ts, "discord_auto_trim", False))


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


def _exit_alert_text(guard: DiscordPositionGuard) -> str:
    """The synthetic alert that fires this rung.

    Spells the contract out in the compact format the parser already reads, and
    marks it with the scissors that make it an EXIT. Writing it as text rather
    than calling execution directly is what keeps this on the one trim path —
    it is read by the same parser that reads the author's own trims.
    """
    right = guard.option_right
    cp = "C" if (right == OptionRight.CALL or right == "call") else "P"
    strike = Decimal(str(guard.option_strike or 0)).normalize()
    return f"✂️ ${guard.symbol} {strike}{cp} {guard.option_expiry:%m/%d}"


def _live_guards(db):
    from sqlalchemy import select  # noqa: PLC0415

    return list(db.execute(
        select(DiscordPositionGuard).where(
            DiscordPositionGuard.closed_at.is_(None),
            DiscordPositionGuard.entry_price.is_not(None),
            # Options only: the alert text this builds names a contract, and
            # the ladder has never run on anything else.
            DiscordPositionGuard.option_strike.is_not(None),
        )
    ).scalars())


def _mark_for(adapter, guard) -> Decimal | None:
    """The broker's own mark for this contract, from the position it holds.

    Read from the POSITION rather than a quote endpoint because Alpaca exposes
    no option quote, and because a mark with no position behind it would fire a
    trim on something already gone.
    """
    try:
        positions = adapter.get_positions()
    except Exception:  # noqa: BLE001
        # A broker hiccup must not fire or skip a rung on bad data.
        log.warning("auto-trim: could not read positions for %s", guard.symbol, exc_info=True)
        return None
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


def tick() -> None:
    """One sweep. Never raises: a bad guard must not stop the others."""
    from app.api.discord_sources import submit_self_alert_text  # noqa: PLC0415
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.models.settings import TraderSettings  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    with SessionLocal() as db:
        guards = _live_guards(db)
        if not guards:
            return
        by_user: dict = {}
        for g in guards:
            by_user.setdefault(g.user_id, []).append(g)

        for user_id, rows in by_user.items():
            ts = db.get(TraderSettings, user_id)
            if not _enabled(ts):
                continue
            user = db.get(User, user_id)
            if user is None:
                continue
            acct = db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == user_id,
                    BrokerAccount.connection_status == "connected",
                )
            ).scalars().first()
            if acct is None:
                continue
            try:
                adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
            except Exception:  # noqa: BLE001
                log.warning("auto-trim: no adapter for user %s", user_id, exc_info=True)
                continue

            for guard in rows:
                try:
                    mark = _mark_for(adapter, guard)
                    rung = due_rung(ts, guard, mark)
                    if rung is None:
                        continue
                    text = _exit_alert_text(guard)
                    log.info(
                        "auto-trim: %s reached %.2f%% — firing trim %s via %r",
                        guard.symbol, gain_pct(guard.entry_price, mark), rung, text,
                    )
                    submit_self_alert_text(db, user, text, approve=True)
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
