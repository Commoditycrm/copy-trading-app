"""Daily P&L limit poller — Alpaca + SnapTrade.

Polls each subscriber's connected broker account on a cadence that's
broker-specific:

  * **Alpaca**: 10s per tick. Each account costs ~6 req/min against
    Alpaca's own 200/min budget — comfortable.
  * **SnapTrade**: 60s per tick (and rate-controlled — see below).
    SnapTrade has a 250 req/min PLATFORM quota that's shared with the
    order listener, so we can't hit it as hard.

Every tick, for every subscriber with a connected supported broker:

  1. Skip-idle gate. If the subscriber has NO active policy (no
     kill-switch, no auto-liquidation, no position TP/SL), don't make
     the broker call at all. This is the single biggest rate-limit
     saver — typical deployments have a long tail of subscribers with
     nothing enforced.
  2. Call ``adapter.get_pnl_snapshot()`` — equity, last_equity (today's
     start-of-day balance), and today's P&L = equity - last_equity.
  3. If the subscriber has a ``daily_loss_limit`` or ``daily_profit_limit``
     set AND today's P&L breaches it, flip ``copy_enabled = False`` and
     stamp ``pnl_auto_paused_at``. Same audit + cache-invalidation + SSE
     event shape the in-fanout enforcement uses.
  4. If ``max_account_pct_per_day`` is set AND today's filled trade
     notional (from our own fills table) crosses
     ``beginning_day_balance * pct/100``, same pause. Skipped silently
     when the broker doesn't expose a day-start.
  5. If ``position_tp_pct`` / ``position_sl_pct`` is set, run the
     per-position enforcer (one additional ``adapter.get_positions()``
     call). Closes any position whose unrealized P&L percent has
     breached either threshold.
  6. Emit a ``pnl.tick`` SSE event so the Settings page's P&L Limit and
     Risk Limits panels update live.

Once a subscriber is auto-paused by any limit (daily P&L, account-pct,
or auto-liquidation), copy stays OFF until they manually re-enable it
from Settings — there is no auto-resume.

SnapTrade rate-limit math
-------------------------
Quota: 250 req/min platform-wide. Typical workload:

  * ``snaptrade_listener`` (per active trader, 5s cadence) ~12 req/min
  * Adapter's ``get_pnl_snapshot`` makes 2 SnapTrade calls internally
    (balance + account details) — so each polled subscriber costs 2
    req per 60s tick = 2 req/min.
  * ``adapter.get_positions`` (only for subs with position TP/SL set):
    1 req per 60s tick = 1 req/min/sub.

Worked example (38 SnapTrade subscribers):
  * Listener: ~12-24 req/min (1-2 traders)
  * Skip-idle reduces actively-polled subs to ~50% → 19 subs × 2 req/min = 38
  * Position-TP/SL on ~25% of subs → 10 × 1 req/min = 10
  * Total: ~60-72 req/min ✓ well under 250

Concurrency is gated by ``_SNAPTRADE_SEM`` so that even if every subscriber
is due on the same tick, only N at a time hit SnapTrade. Prevents the
burst-of-38-HTTP-requests problem.

Why a separate task (not piggyback on copy_engine)
--------------------------------------------------
copy_engine's check only runs when a trader fanouts. If the trader is
quiet for hours but the subscriber's positions move against them, the
limit goes un-policed. This poller fills that gap.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.settings import SubscriberSettings
from app.services import audit, cache, events
from app.services.crypto import decrypt_json
from app.services import pnl
from app.services.pnl import today_filled_notional

log = logging.getLogger(__name__)


# Outer loop tick. This is the FLOOR cadence — actual per-account
# intervals can be longer (see ``_interval_for_broker``) but never
# shorter than this. Set to the minimum admin-allowed Alpaca interval
# (1s) so a runtime override down to 1s actually fires every second
# instead of being clipped to the old floor. The loop only does real
# work for accounts whose timer has elapsed; on idle wakes it's a
# single SELECT + dict comparison, sub-millisecond.
POLL_INTERVAL_S = 1.0

# Per-broker minimum interval (seconds). Kept as a static fallback for
# brokers without a runtime knob. SnapTrade gets 60s because its 250
# req/min platform quota is shared with the order listener — see the
# rate-limit math in the module docstring. The ALPACA entry is the
# fallback used when Redis is unreachable; the live value comes from
# ``platform_config.get_alpaca_pnl_poll_interval_sync()``.
_INTERVAL_BY_BROKER: dict[BrokerName, float] = {
    BrokerName.ALPACA: 10.0,
    BrokerName.SNAPTRADE: 60.0,
}


def _interval_for_broker(broker: BrokerName) -> float:
    """Return the current per-tick interval for ``broker``, reading any
    runtime override every call. For Alpaca this checks Redis (~0.5ms);
    for everything else it uses the static map above.

    Called on the hot path (stamping the next-due time after each tick)
    so an admin change lands on the very next tick without restart."""
    if broker == BrokerName.ALPACA:
        try:
            from app.services.platform_config import (  # noqa: PLC0415
                get_alpaca_pnl_poll_interval_sync,
            )
            return float(get_alpaca_pnl_poll_interval_sync())
        except Exception:  # noqa: BLE001
            log.warning(
                "pnl_poller: alpaca interval lookup failed; using fallback %ss",
                _INTERVAL_BY_BROKER[BrokerName.ALPACA],
            )
            return _INTERVAL_BY_BROKER[BrokerName.ALPACA]
    return _INTERVAL_BY_BROKER.get(broker, POLL_INTERVAL_S)

# Per-account monotonic timestamp of the earliest time the account is
# allowed to be polled again. The outer loop ticks every POLL_INTERVAL_S
# and uses this dict to skip accounts that aren't yet due. Entries for
# deleted accounts hang around but are harmless — just stale keys.
_next_due_at: dict[uuid.UUID, float] = {}

# Brokers the poller knows how to fetch from. Adding a broker is:
# (a) the adapter implements ``get_pnl_snapshot()``, and (b) the broker
# is listed here, and (c) the broker has an entry in
# ``_INTERVAL_BY_BROKER``.
_SUPPORTED_BROKERS = (BrokerName.ALPACA, BrokerName.SNAPTRADE)

# Concurrency gate for SnapTrade. Even with skip-idle and 60s cadence,
# if 30+ subscribers all become due in the same tick they would burst
# fire HTTP requests concurrently and trip SnapTrade's instantaneous
# rate-limiter (separate from the 250/min platform quota). The
# semaphore caps simultaneous in-flight calls so the burst spreads out
# over the tick. Sized small (4) because each ``_enforce_one`` makes
# 2-3 SnapTrade calls; a higher cap risks 429.
_SNAPTRADE_SEM = threading.Semaphore(4)


_task: asyncio.Task | None = None
_main_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def start() -> None:
    """Spawn the single polling task. Idempotent — calling twice is a no-op."""
    global _task
    if _task and not _task.done():
        return
    if _main_loop is None:
        log.warning("pnl_poller: no main loop bound; start is a no-op")
        return
    _task = _main_loop.create_task(_run())
    # Log the live effective intervals so the startup line reflects any
    # current admin override. Alpaca goes through the runtime lookup;
    # other brokers come from the static map.
    parts = []
    for broker in _SUPPORTED_BROKERS:
        parts.append(f"{broker.value}={_interval_for_broker(broker):.0f}s")
    log.info(
        "pnl_poller: started (tick=%.0fs, %s)", POLL_INTERVAL_S, ", ".join(parts),
    )


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def _run() -> None:
    """Outer loop ticks every POLL_INTERVAL_S. On each tick:

      1. Load every connected broker_account (supported brokers only).
      2. Filter to accounts whose ``_next_due_at`` has elapsed — that's
         how SnapTrade (60s cadence) is polled less often than Alpaca
         (10s) without spawning separate tasks per broker.
      3. Fan ``_enforce_one`` out concurrently over the threadpool
         (asyncio.gather → wall-clock = slowest single call).
      4. Stamp the next-due time per account using its broker's interval.
    """
    while True:
        try:
            accts = await asyncio.to_thread(_load_active_accounts)
            now = time.monotonic()
            due = [a for a in accts if _next_due_at.get(a.id, 0.0) <= now]
            if due:
                await asyncio.gather(
                    *(asyncio.to_thread(_enforce_one_safe, acct) for acct in due),
                    return_exceptions=True,
                )
                # Stamp next-due AFTER the work — using a fresh monotonic
                # read so a slow tick (e.g. SnapTrade throttling) doesn't
                # immediately re-run the same accounts on the next tick.
                # ``_interval_for_broker`` reads the live Redis override
                # each call so an admin change picks up on the very next
                # account that gets stamped.
                stamp = time.monotonic()
                for a in due:
                    interval = _interval_for_broker(a.broker)
                    _next_due_at[a.id] = stamp + interval
        except asyncio.CancelledError:
            log.info("pnl_poller: cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("pnl_poller: tick failed")
        await asyncio.sleep(POLL_INTERVAL_S)


def _load_active_accounts() -> list[BrokerAccount]:
    """Snapshot every connected broker_account whose broker has a
    ``get_pnl_snapshot`` implementation. Detaches the rows from the
    session so ``_enforce_one`` can read scalar attributes after the
    session closes without DetachedInstanceError."""
    with SessionLocal() as db:
        accts = list(db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker.in_(_SUPPORTED_BROKERS),
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())
        for a in accts:
            db.expunge(a)
        return accts


def _has_active_policies(s: SubscriberSettings) -> bool:
    """Return True if the subscriber has at least one enforcement policy
    configured. When False, the poller can skip the broker call entirely
    — there's nothing to enforce, and the pnl.tick event would only be
    consumed by the Settings page to show progress against limits that
    don't exist. The biggest single rate-limit saver on SnapTrade."""
    return any((
        s.daily_loss_limit is not None,
        s.daily_profit_limit is not None,
        s.daily_loss_limit_pct is not None,
        s.daily_profit_limit_pct is not None,
        s.max_account_pct_per_day is not None,
        s.auto_liquidation_limit is not None,
        s.position_tp_pct is not None,
        s.position_sl_pct is not None,
    ))


def _account_role(user_id: uuid.UUID) -> tuple[str, bool]:
    """Return (role, should_run) for the user that owns this account.

    ``role`` is one of "subscriber" | "trader" | "other". ``should_run``
    is the skip-idle gate:
      * subscriber → True iff at least one enforcement policy is set
      * trader     → True iff there's at least one open option-entry
                     row with stop_loss_price set (trader option SL
                     monitor is the only trader-side work here)
      * other      → False (admin accounts have nothing to enforce)

    One short read-only DB session — kept off the hot path so the
    broker call only happens when there's actually work to do.
    """
    from app.models.user import User, UserRole  # noqa: PLC0415
    from app.models.order import (  # noqa: PLC0415
        InstrumentType, Order, OrderStatus,
    )
    from sqlalchemy import select as _select  # noqa: PLC0415

    with SessionLocal() as db:
        u = db.get(User, user_id)
        if u is None:
            return ("other", False)
        if u.role == UserRole.SUBSCRIBER:
            s = db.get(SubscriberSettings, user_id)
            if s is None:
                return ("subscriber", False)
            return ("subscriber", _has_active_policies(s))
        if u.role == UserRole.TRADER:
            # Skip-idle for the trader monitor: only worth a broker
            # call if there's at least one filled option entry with
            # an SL price still possibly open. The monitor itself
            # double-checks per-position; this gate just avoids the
            # broker call for traders with no option SL setups at all.
            has_setup = db.execute(
                _select(Order.id).where(
                    Order.user_id == user_id,
                    Order.instrument_type == InstrumentType.OPTION,
                    Order.status == OrderStatus.FILLED,
                    Order.is_closing.is_(False),
                    Order.parent_order_id.is_(None),
                    Order.bracket_parent_id.is_(None),
                    Order.stop_loss_price.isnot(None),
                ).limit(1)
            ).scalar_one_or_none() is not None
            return ("trader", has_setup)
        return ("other", False)


def _enforce_one_safe(acct: BrokerAccount) -> None:
    """Crash isolation wrapper. Routes to subscriber kill-switches +
    position-TP/SL enforcement for subscribers, and to the option-SL
    price-monitor for traders. Both run inside the same SnapTrade
    semaphore so total in-flight calls stay bounded.

    Layered rate-limit protection:
      1. Skip-idle: no work this tick if there's nothing to enforce.
         Avoids a 2-3 call burst per idle account.
      2. Concurrency semaphore: caps simultaneous SnapTrade calls.
    """
    role, should_run = _account_role(acct.user_id)
    if not should_run:
        return

    if acct.broker == BrokerName.SNAPTRADE:
        with _SNAPTRADE_SEM:
            _enforce_one_inner(acct, role)
        return
    _enforce_one_inner(acct, role)


def _enforce_one_inner(acct: BrokerAccount, role: str) -> None:
    try:
        if role == "trader":
            _enforce_one_trader(acct)
        else:
            _enforce_one(acct)
    except Exception:  # noqa: BLE001
        log.exception(
            "pnl_poller: enforce failed for account %s (user %s, role=%s)",
            acct.id, acct.user_id, role,
        )


def _enforce_one_trader(acct: BrokerAccount) -> None:
    """Trader-side tick: run the option SL price monitor and publish
    any triggered closes. Lightweight wrapper because the monitor
    already commits + audits per-close — we just translate its
    return value into SSE + persistent notifications so the trader
    sees the SL fire in real time.
    """
    from app.services import events as _events  # noqa: PLC0415
    from app.services.trader_bracket_monitor import (  # noqa: PLC0415
        enforce_trader_option_sl,
    )

    pending_events: list[dict[str, Any]] = []
    notifications_to_send: list[dict[str, Any]] = []

    with SessionLocal() as db:
        closures = enforce_trader_option_sl(db, acct.user_id, acct.id)
        if not closures:
            return
        for c in closures:
            pending_events.append({
                "type": "trader.bracket_sl_triggered",
                "symbol": c["symbol"],
                "qty": c["qty"],
                "sl_price": c["sl_price"],
                "mark": c["mark"],
                "limit": c["limit"],
                "broker_order_id": c["broker_order_id"],
                "entry_order_id": c["entry_order_id"],
            })
            # Append realised-P&L % so the trader knows the impact at a
            # glance — "Closed for -7.3%" is more actionable than just
            # "Closed at $0.85". Signed (negative for SL hits as
            # expected); the helper that computed it already chose the
            # right sign per long/short.
            pnl_pct = c.get("pnl_pct")
            pnl_phrase = f" Realised P&L: {pnl_pct}%." if pnl_pct is not None else ""
            notifications_to_send.append({
                "type": "bracket.sl_triggered",
                "message": (
                    f"{c['symbol']} option SL hit at ${c['mark']} "
                    f"(threshold ${c['sl_price']}). "
                    f"Close placed at ${c['limit']} — {c['qty']} contracts."
                    f"{pnl_phrase}"
                ),
                "metadata": c,
            })
        db.commit()

    # Persist notifications + publish SSE in their own session so
    # commit ordering is clean (state durable before events go out).
    if notifications_to_send:
        with SessionLocal() as db:
            from app.services import notifications as notif_svc  # noqa: PLC0415
            for n in notifications_to_send:
                try:
                    notif_svc.create_notification(
                        db,
                        user_id=acct.user_id,
                        type=n["type"],
                        message=n["message"],
                        metadata=n["metadata"],
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "trader_bracket_monitor: notification failed for user=%s",
                        acct.user_id,
                    )
            db.commit()

    for evt in pending_events:
        _events.publish(acct.user_id, evt)


def _enforce_one(acct: BrokerAccount) -> None:
    """One subscriber's tick. Opens its own session so the commit lands
    BEFORE the SSE events go out — the frontend's refetch on
    ``copy.auto_paused`` would otherwise race the poller's transaction
    and read the pre-pause state, leaving the toggle visually stuck on
    until the next manual refresh."""
    pending_events: list[dict[str, Any]] = []

    # Fetch the broker P&L snapshot FIRST — with NO DB session/connection held.
    # SnapTrade rate-limits (429) and this call can block for seconds; doing it
    # inside `with SessionLocal()` pinned a pool connection in "idle in
    # transaction" for the whole call. With every connected account polled
    # concurrently every tick, that exhausted the pool (15/15) and stalled all
    # DB-backed APIs. _fetch_pnl_snapshot only needs `acct` (no DB), so do it
    # up front and only open the session for the fast enforcement writes.
    state = _fetch_pnl_snapshot(acct)

    with SessionLocal() as db:
        s = db.get(SubscriberSettings, acct.user_id)
        if s is None:
            # The Alpaca account belongs to an admin or trader (no
            # SubscriberSettings row) — nothing to enforce here.
            return

        now_utc = datetime.now(timezone.utc)
        invalidate_trader_id: uuid.UUID | None = None

        # today's P&L snapshot (todays_pl / equity / beginning_day_balance) was
        # fetched ABOVE, before this session opened, so we never hold a pool
        # connection across the slow broker call. beginning_day_balance may be
        # None for SnapTrade brokers that don't expose a day-start figure; the
        # pct kill switch is silently skipped for those subscribers.
        if state is None:
            # Broker call failed — nothing to commit; try again next tick.
            return
        todays_pl = state["todays_pl"]
        equity = state["equity"]
        beginning_day_balance: Decimal | None = state.get("beginning_day_balance")

        # ── Broker-agnostic day-start fallback ────────────────────────────
        # Direct Alpaca returns `last_equity` (yesterday's market close)
        # so beginning_day_balance is populated and we trust it.
        # SnapTrade-routed Alpaca paper (and some other SnapTrade
        # brokerages) don't expose a day-start field — beginning_day_balance
        # comes back None. Without it, todays_pl can't be computed (the
        # adapter falls back to 0) and every pct-based kill switch
        # silently disables.
        #
        # We close that gap with our own snapshot: the first poll of
        # each UTC day per account records `equity` as the day-start;
        # subsequent polls that day read it back. Idempotent against
        # racing pollers via the (broker_account_id, utc_date) unique
        # constraint.
        if beginning_day_balance is None and equity is not None:
            try:
                from app.services.day_start_equity import get_or_record  # noqa: PLC0415
                beginning_day_balance = get_or_record(db, acct.id, equity)
                # Recompute todays_pl using our recorded baseline.
                todays_pl = equity - beginning_day_balance
            except Exception:  # noqa: BLE001
                # Snapshot table failure must not block the rest of the
                # tick — kill-switch math will silently skip pct rules
                # this tick, same as before this fallback existed.
                log.exception(
                    "pnl_poller: day-start snapshot failed for account %s",
                    acct.id,
                )

        # ── Pct-of-day-start-balance TRADING-VALUE cap ───────────────────
        # Tracks today's cumulative filled trade notional (capital
        # deployed, not P&L). When today's trading USD crosses
        # beginning_day_balance * pct/100, copy is paused. Using the
        # day-start balance means the dollar threshold is FIXED for the
        # trading day; if we used live equity, the threshold would drift
        # up on gains and down on losses, which would be confusing.
        todays_trading_value = today_filled_notional(db, s.user_id)

        pct_limit_dollars: Decimal | None = None
        if (
            s.max_account_pct_per_day is not None
            and beginning_day_balance is not None
            and beginning_day_balance > 0
        ):
            pct_limit_dollars = beginning_day_balance * s.max_account_pct_per_day / Decimal(100)

        # Legacy USD-based daily kill switches. Stay enforced for accounts
        # still configured with absolute amounts (predate the % rollout).
        hit_loss = (
            s.daily_loss_limit is not None and todays_pl <= -s.daily_loss_limit
        )
        hit_profit = (
            s.daily_profit_limit is not None and todays_pl >= s.daily_profit_limit
        )

        # New PERCENTAGE-of-day-start daily limits. Derive the dollar
        # threshold each tick from beginning_day_balance, then trip on
        # the same realized-P&L breach as the USD variants. Skipped when
        # beginning_day_balance is unavailable (some SnapTrade brokers).
        loss_pct_dollars: Decimal | None = None
        profit_pct_dollars: Decimal | None = None
        if beginning_day_balance is not None and beginning_day_balance > 0:
            if s.daily_loss_limit_pct is not None:
                loss_pct_dollars = beginning_day_balance * s.daily_loss_limit_pct / Decimal(100)
            if s.daily_profit_limit_pct is not None:
                profit_pct_dollars = beginning_day_balance * s.daily_profit_limit_pct / Decimal(100)
        hit_loss_pct = (
            loss_pct_dollars is not None and todays_pl <= -loss_pct_dollars
        )
        hit_profit_pct = (
            profit_pct_dollars is not None and todays_pl >= profit_pct_dollars
        )

        hit_pct = (
            pct_limit_dollars is not None and todays_trading_value >= pct_limit_dollars
        )
        # Auto-liquidation: a take-profit ceiling on UNREALIZED P&L for
        # the day (today's total mark-to-market gains on still-open
        # positions). Distinct from daily_profit_limit which is a REALIZED
        # circuit breaker triggered by closed fills. When tripped we
        # (1) flip copy_enabled, (2) stamp auto_liquidated_at as an audit
        # marker, (3) hand off to auto_liquidator to flatten the broker —
        # which converts the unrealized gain into a realized one.
        #
        #   unrealized = todays_total_pl − today_realized_pnl
        #              = (equity − beginning_day_balance) − fills_today
        #
        # Requires beginning_day_balance to be known (some SnapTrade
        # brokers don't expose it — for those subscribers the take-profit
        # check is skipped, same as the pct kill switch).
        todays_realized = pnl.today_realized_pnl(db, s.user_id)
        unrealized_pl: Decimal | None = None
        if beginning_day_balance is not None:
            unrealized_pl = todays_pl - todays_realized
        hit_liquidation = (
            s.auto_liquidation_limit is not None
            and s.auto_liquidation_limit > 0
            and unrealized_pl is not None
            and unrealized_pl >= s.auto_liquidation_limit
        )

        if s.copy_enabled and hit_liquidation:
            from app.services.auto_liquidator import liquidate_subscriber_account  # noqa: PLC0415
            s.copy_enabled = False
            s.auto_liquidated_at = now_utc
            try:
                liq_summary = liquidate_subscriber_account(db, s.user_id, acct.id)
            except Exception:  # noqa: BLE001
                log.exception("pnl_poller: liquidation crashed for user=%s", s.user_id)
                liq_summary = {"cancelled": 0, "closed": 0, "failures": [{"error": "crashed"}]}
            audit.record(
                db, actor_user_id=s.user_id,
                action="copy.auto_liquidated_take_profit",
                entity_type="subscriber_settings", entity_id=s.user_id,
                metadata={
                    "source": "pnl_poller",
                    "broker": acct.broker.value,
                    "equity": str(equity),
                    "unrealized_pl": str(unrealized_pl) if unrealized_pl is not None else None,
                    "todays_realized_pnl": str(todays_realized),
                    "todays_total_pl": str(todays_pl),
                    "auto_liquidation_limit": str(s.auto_liquidation_limit),
                    "cancelled": liq_summary.get("cancelled"),
                    "closed":    liq_summary.get("closed"),
                    "failures":  liq_summary.get("failures"),
                },
            )
            if s.following_trader_id:
                invalidate_trader_id = s.following_trader_id
            pending_events.append({
                "type": "copy.auto_liquidated",
                "reason": "auto_liquidation_take_profit",
                "auto_liquidation_limit": str(s.auto_liquidation_limit),
                "unrealized_pl": str(unrealized_pl) if unrealized_pl is not None else None,
                "equity": str(equity),
                "cancelled": liq_summary.get("cancelled"),
                "closed":    liq_summary.get("closed"),
            })
            try:
                from app.services import notifications as notif_svc  # noqa: PLC0415
                notif_svc.create_notification(
                    db,
                    user_id=s.user_id,
                    type="copy.auto_liquidated",
                    message=(
                        f"Unrealized profit hit ${unrealized_pl} "
                        f"(target ${s.auto_liquidation_limit}). "
                        f"All positions closed to lock in the gain; "
                        f"copy trading is OFF until you turn it back on."
                    ),
                    metadata={
                        "equity": str(equity),
                        "unrealized_pl": str(unrealized_pl) if unrealized_pl is not None else None,
                        "auto_liquidation_limit": str(s.auto_liquidation_limit),
                        "cancelled": liq_summary.get("cancelled"),
                        "closed":    liq_summary.get("closed"),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("pnl_poller: notification failed for user=%s", s.user_id)

        if s.copy_enabled and (hit_loss or hit_profit or hit_pct or hit_loss_pct or hit_profit_pct):
            if hit_loss:
                reason = "daily_loss_limit"
            elif hit_profit:
                reason = "daily_profit_limit"
            elif hit_loss_pct:
                reason = "daily_loss_limit_pct"
            elif hit_profit_pct:
                reason = "daily_profit_limit_pct"
            else:
                reason = "max_account_pct_per_day"
            s.copy_enabled = False
            s.pnl_auto_paused_at = now_utc
            audit.record(
                db, actor_user_id=s.user_id,
                action=f"copy.auto_paused_{reason}",
                entity_type="subscriber_settings", entity_id=s.user_id,
                metadata={
                    "source":                  "pnl_poller",
                    "broker":                  acct.broker.value,
                    "todays_pl":               str(todays_pl),
                    "todays_trading_value":    str(todays_trading_value),
                    "equity":                  str(equity),
                    "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
                    "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
                    "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
                    "daily_loss_limit_pct":    str(s.daily_loss_limit_pct) if s.daily_loss_limit_pct else None,
                    "daily_profit_limit_pct":  str(s.daily_profit_limit_pct) if s.daily_profit_limit_pct else None,
                    "loss_pct_dollars":        str(loss_pct_dollars) if loss_pct_dollars is not None else None,
                    "profit_pct_dollars":      str(profit_pct_dollars) if profit_pct_dollars is not None else None,
                    "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
                    "pct_limit_dollars":       str(pct_limit_dollars) if pct_limit_dollars is not None else None,
                },
            )
            if s.following_trader_id:
                invalidate_trader_id = s.following_trader_id
            # Reuses the existing `copy.auto_paused` event shape that the
            # Settings page already listens to (toasts the pause notice).
            pending_events.append({
                "type": "copy.auto_paused",
                "reason": reason,
                "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
                "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
                "daily_loss_limit_pct":    str(s.daily_loss_limit_pct) if s.daily_loss_limit_pct else None,
                "daily_profit_limit_pct":  str(s.daily_profit_limit_pct) if s.daily_profit_limit_pct else None,
                "loss_pct_dollars":        str(loss_pct_dollars) if loss_pct_dollars is not None else None,
                "profit_pct_dollars":      str(profit_pct_dollars) if profit_pct_dollars is not None else None,
                "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
                "pct_limit_dollars":       str(pct_limit_dollars) if pct_limit_dollars is not None else None,
                "todays_realized_pnl":     str(todays_pl),
                "todays_trading_value":    str(todays_trading_value),
                "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
            })

        # ── Per-position TP/SL enforcement ────────────────────────────────
        # Independent of the daily kill switches above — fires whenever
        # any open position's unrealized P&L percent breaches the
        # subscriber's per-position TP or SL. Per-position only: a
        # triggered close does NOT pause copy_enabled, and other
        # positions continue to be managed normally.
        if s.position_tp_pct is not None or s.position_sl_pct is not None:
            try:
                from app.services.position_enforcer import (  # noqa: PLC0415
                    enforce_position_tp_sl,
                )
                closures = enforce_position_tp_sl(db, s.user_id, acct.id)
            except Exception:  # noqa: BLE001
                log.exception(
                    "pnl_poller: position_enforcer crashed for user=%s", s.user_id
                )
                closures = []
            for c in closures:
                pending_events.append({
                    "type": "position.auto_closed",
                    "leg": c["leg"],
                    "symbol": c["symbol"],
                    "qty": c["qty"],
                    "pct": c["pct"],
                    "position_tp_pct":
                        str(s.position_tp_pct) if s.position_tp_pct is not None else None,
                    "position_sl_pct":
                        str(s.position_sl_pct) if s.position_sl_pct is not None else None,
                    "broker": acct.broker.value,
                })
                try:
                    from app.services import notifications as notif_svc  # noqa: PLC0415
                    leg_label = "take-profit" if c["leg"] == "tp" else "stop-loss"
                    threshold = (
                        s.position_tp_pct if c["leg"] == "tp" else s.position_sl_pct
                    )
                    notif_svc.create_notification(
                        db,
                        user_id=s.user_id,
                        type=f"position.auto_closed_{c['leg']}",
                        message=(
                            f"{c['symbol']} closed automatically at {c['pct']}% "
                            f"({leg_label} threshold {threshold}%). "
                            f"Other positions and copy trading are unaffected."
                        ),
                        metadata={
                            "symbol": c["symbol"],
                            "leg": c["leg"],
                            "pct": c["pct"],
                            "qty": c["qty"],
                            "broker": acct.broker.value,
                            "broker_order_id": c["broker_order_id"],
                        },
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "pnl_poller: position TP/SL notification failed for user=%s",
                        s.user_id,
                    )

        # ── Commit BEFORE any events go out ───────────────────────────────
        # Commit only when there were actual mutations — pnl.tick alone
        # is a pure read and shouldn't bump audit timestamps. We snapshot
        # the values needed for the tick payload BEFORE the commit so the
        # publish below isn't reading post-commit ORM state.
        tick_payload = {
            "type":                    "pnl.tick",
            "todays_realized_pnl":     str(todays_pl),
            "todays_trading_value":    str(todays_trading_value),
            "equity":                  str(equity),
            "beginning_day_balance":   str(beginning_day_balance) if beginning_day_balance is not None else None,
            "daily_loss_limit":        str(s.daily_loss_limit) if s.daily_loss_limit else None,
            "daily_profit_limit":      str(s.daily_profit_limit) if s.daily_profit_limit else None,
            "daily_loss_limit_pct":    str(s.daily_loss_limit_pct) if s.daily_loss_limit_pct else None,
            "daily_profit_limit_pct":  str(s.daily_profit_limit_pct) if s.daily_profit_limit_pct else None,
            "loss_pct_dollars":        str(loss_pct_dollars) if loss_pct_dollars is not None else None,
            "profit_pct_dollars":      str(profit_pct_dollars) if profit_pct_dollars is not None else None,
            "max_account_pct_per_day": str(s.max_account_pct_per_day) if s.max_account_pct_per_day else None,
            "max_per_contract":        str(s.max_per_contract) if s.max_per_contract else None,
            "auto_liquidation_limit":  str(s.auto_liquidation_limit) if s.auto_liquidation_limit else None,
            "position_tp_pct":         str(s.position_tp_pct) if s.position_tp_pct is not None else None,
            "position_sl_pct":         str(s.position_sl_pct) if s.position_sl_pct is not None else None,
            # Today's unrealized P&L = total daily P&L − realized fills.
            # The take-profit auto-liquidation triggers when this crosses
            # ``auto_liquidation_limit``; surface it on the tick so the
            # Settings page can render headroom + progress live.
            "unrealized_pl":           str(unrealized_pl) if unrealized_pl is not None else None,
            "copy_enabled":            s.copy_enabled,
        }
        user_id_snapshot = s.user_id
        # Commit when there's anything to persist. pending_events is the
        # usual signal (kill-switch trips, position closures), but the
        # enforcer can also write REJECTED Order rows + audit records
        # when the broker rejects a close — those need to be persisted
        # even when closures came back empty so we have an audit trail
        # of the failed attempt. db.dirty / db.new pick that up.
        if pending_events or db.new or db.dirty:
            db.commit()

    # ── Outside the session: publish events + invalidate caches ──────────
    # Doing this AFTER the session closes guarantees the transaction is
    # durable — the frontend's refetch on `copy.auto_paused` will read
    # the committed row and the UI toggle reflects the pause instantly.
    if invalidate_trader_id:
        _safe_invalidate(invalidate_trader_id)
    _flush(user_id_snapshot, pending_events)
    events.publish(user_id_snapshot, tick_payload)


def _safe_invalidate(trader_id: uuid.UUID) -> None:
    """Bust the trader's subscriber cache; swallow Redis hiccups so a
    transient cache outage never blocks the per-subscriber loop."""
    try:
        cache.invalidate_subscribers_for_trader(trader_id)
    except Exception:  # noqa: BLE001
        log.warning("pnl_poller: cache invalidate failed for trader %s", trader_id)


def _flush(user_id: uuid.UUID, pending: list[dict[str, Any]]) -> None:
    for evt in pending:
        events.publish(user_id, evt)


def _fetch_pnl_snapshot(acct: BrokerAccount) -> dict[str, Any] | None:
    """Broker-agnostic fetch. Decrypts creds, builds the right adapter
    via ``adapter_for``, calls ``get_pnl_snapshot``. Returns the dict
    shape ``{"todays_pl", "equity", "beginning_day_balance"}`` or None
    on any failure (caller skips the tick). ``beginning_day_balance``
    inside the dict may itself be None for SnapTrade brokers that don't
    expose a day-start — the pct kill switch is skipped in that case
    while the loss/profit limits and live tile still work."""
    try:
        creds = decrypt_json(acct.encrypted_credentials)
    except Exception:  # noqa: BLE001
        log.exception("pnl_poller: decrypt failed for account %s", acct.id)
        return None
    try:
        adapter = adapter_for(acct, creds)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pnl_poller: adapter_for failed for account %s: %s", acct.id, exc,
        )
        return None
    try:
        return adapter.get_pnl_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pnl_poller: %s get_pnl_snapshot failed for account %s: %s",
            adapter.name, acct.id, exc,
        )
        return None


__all__ = ["bind_loop", "start", "stop", "POLL_INTERVAL_S"]
