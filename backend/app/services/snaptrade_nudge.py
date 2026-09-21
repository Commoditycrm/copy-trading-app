"""On-placement fill nudge for SnapTrade subscriber mirror orders.

The problem
-----------
SnapTrade does not stream fills. It CACHES brokerage data and re-pulls from the
upstream broker on its own background cadence, and everything we read —
``list_recent_activities`` / ``get_user_account_orders`` — serves that cache. So
a subscriber's mirror order can be filled at the broker minutes before SnapTrade
will admit it, which is exactly what subscribers report: "filled in my broker,
still pending in the app".

Measured on prod over 7 days (submit → our first observed fill, SnapTrade
subscriber mirrors only)::

    p50   50s        0-30s     84 orders
    p90 1337s       30-120s    68
    max  ~5.4d       2-10m     40
                    10-60m     30
                     >1hr      20

That is a smooth decay, not a step at our 30s sweep interval — 65% of fills land
AFTER the point where a faster sweep could have found them. Polling harder reads
the same stale cache more often; it cannot make SnapTrade hold data it has not
fetched yet. Throttling is not the cause either (3 throttled orders in the same
7 days).

What this does
--------------
``SnapTradeAdapter.force_resync()`` asks SnapTrade to re-pull from the brokerage
NOW instead of waiting for its cadence. Until now it was called from ONE place:
``snaptrade_listener._should_force_resync``, which filters
``Order.parent_order_id.is_(None)`` (trader orders) and runs only inside the
per-TRADER poll loop. Subscribers have neither — so in the entire system nothing
ever nudged a subscriber's connection, and every subscriber fill waited on
SnapTrade's own schedule. That is the gap this closes.

Shape, per mirror placement on a SnapTrade subscriber account:

    t+0s   force_resync()                   — 1 call, tell SnapTrade to fetch
    t+4s   light reconcile (activities)     — 1 call, read what it fetched
    t+10s  light reconcile, if still working — 1 call

The refresh is ASYNCHRONOUS on SnapTrade's side, which is why the read is
deferred rather than issued alongside it: reading at t+0 would just re-read the
same cache we are asking it to replace.

Cost control
------------
SnapTrade's quota (~250 req/min) is a SINGLE PLATFORM-WIDE pool shared by every
user, unlike Webull's per-app_key limits — so a fanout to N subscribers costs N
times everything. Three guards keep this bounded:

* **One worker thread.** All nudge traffic is serialised, so a 21-subscriber
  fanout drips instead of bursting 21 connections at once.
* **A token bucket** (``_BUDGET_PER_MIN``). Nudging can never consume more than
  its share of the quota; over budget, the nudge is dropped and the order simply
  falls back to the ordinary 30s sweep. Degrades to today's behaviour, never
  worse.
* **A LIGHT read.** The 30s sweep's full reconcile costs one ``get_order`` per
  working order plus a ``get_positions`` for its recovery guard. The nudge
  passes ``light=True`` to skip both, so it is exactly ONE call. The skipped
  work is recovery of already-terminal rows, which is not time-critical and the
  next sweep does anyway.

Worst case for a 21-subscriber fanout: 21 resyncs + up to 42 reads = 63 calls
spread over ~12s, bucket permitting.

Safety
------
It only ever reads and writes what the broker reports, through the same
``_persist_subscriber_fill`` the 30s sweep uses (which refuses to downgrade a
real fill and refuses to recover against an unreachable broker). It never infers
or fabricates a fill: an order SnapTrade has not confirmed stays exactly as it
is. Every failure is swallowed and logged — the sweep remains the backstop.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Seconds after placement before the first read. SnapTrade's refresh is async;
# this is the grace we give it to land before we look.
READ_DELAY_S = 4.0
# Gap before the follow-up read, when the mirror is still working after the first.
READ_RETRY_S = 6.0
# Reads per nudge (after those, the 30s sweep takes over).
MAX_READS = 2
# Minimum spacing between any two nudge-driven SnapTrade calls.
MIN_GAP_S = 0.15
# Don't re-resync the same connection more often than this — matches the
# listener's SNAPTRADE_RESYNC_MIN_INTERVAL_SEC default.
RESYNC_MIN_INTERVAL_S = 60.0
# Token bucket: the most SnapTrade calls per minute nudging may consume.
_BUDGET_PER_MIN = 90.0


@dataclass
class _Nudge:
    user_id: uuid.UUID
    account_id: uuid.UUID
    due_at: float                 # time.monotonic()
    # default_factory, not a plain default: a dataclass default is bound at
    # class-creation time, which would freeze MAX_READS and diverge from the
    # top-up in schedule() that reads the module global live.
    reads_left: int = field(default_factory=lambda: MAX_READS)
    resync_done: bool = False
    # Built once on the first stage and reused for the rest of this nudge —
    # constructing it costs a BrokerAccount load plus a credential decrypt, and
    # a nudge touches the same connection two or three times in ~10s.
    adapter: object | None = None


@dataclass
class _Bucket:
    """Plain token bucket. Refills continuously at _BUDGET_PER_MIN."""
    tokens: float = _BUDGET_PER_MIN
    updated: float = field(default_factory=time.monotonic)

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(
            _BUDGET_PER_MIN,
            self.tokens + (now - self.updated) * (_BUDGET_PER_MIN / 60.0),
        )
        self.updated = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


_cv = threading.Condition()
_pending: dict[uuid.UUID, _Nudge] = {}     # account_id -> nudge (deduped)
_inflight: set[uuid.UUID] = set()
_last_resync: dict[uuid.UUID, float] = {}  # account_id -> monotonic
_bucket = _Bucket()
_thread: threading.Thread | None = None
_enabled = True


def schedule(user_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Queue a fill nudge for one SnapTrade subscriber account. Thread-safe,
    non-blocking, deduped per account, and never raises — callers are on the
    order-placement hot path and must not care whether this works.

    Callers do NOT need to check the broker type; non-SnapTrade accounts are
    filtered out by the worker when it loads the account."""
    if not _enabled or user_id is None or account_id is None:
        return
    try:
        from app.config import get_settings  # noqa: PLC0415
        if not get_settings().snaptrade_fill_nudge_enabled:
            return
    except Exception:  # noqa: BLE001
        return
    try:
        with _cv:
            _ensure_worker()
            existing = _pending.get(account_id)
            if existing is not None:
                # Another mirror for the same account this fanout. Keep the
                # earlier deadline and top the read budget back up rather than
                # queueing a second nudge for the same connection.
                existing.reads_left = MAX_READS
                return
            _pending[account_id] = _Nudge(
                user_id=user_id,
                account_id=account_id,
                due_at=time.monotonic(),
            )
            _cv.notify()
    except Exception:  # noqa: BLE001
        log.debug("snaptrade nudge: schedule failed", exc_info=True)


def _ensure_worker() -> None:
    """Start the worker thread on first use. Caller holds _cv."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(
        target=_run, name="snaptrade-nudge", daemon=True
    )
    _thread.start()
    log.info(
        "snaptrade fill nudge: worker started "
        "(read at +%.0fs, %d reads max, budget %.0f calls/min)",
        READ_DELAY_S, MAX_READS, _BUDGET_PER_MIN,
    )


def _run() -> None:
    while True:
        try:
            nudge = _claim_next()
        except Exception:  # noqa: BLE001
            log.exception("snaptrade nudge: claim failed")
            time.sleep(1.0)
            continue
        try:
            requeue = _step(nudge)
        except Exception:  # noqa: BLE001
            log.exception(
                "snaptrade nudge: account %s failed", nudge.account_id
            )
            requeue = False
        with _cv:
            _inflight.discard(nudge.account_id)
            if requeue and nudge.account_id not in _pending:
                _pending[nudge.account_id] = nudge
                _cv.notify()
        time.sleep(MIN_GAP_S)


def _claim_next() -> _Nudge:
    """Block until a nudge is due, then take it off the queue."""
    with _cv:
        while True:
            ready = [
                n for n in _pending.values()
                if n.account_id not in _inflight
            ]
            if not ready:
                _cv.wait(timeout=5.0)
                continue
            soonest = min(ready, key=lambda n: n.due_at)
            wait = soonest.due_at - time.monotonic()
            if wait > 0:
                _cv.wait(timeout=min(wait, 5.0))
                continue
            _pending.pop(soonest.account_id, None)
            _inflight.add(soonest.account_id)
            return soonest


def _step(nudge: _Nudge) -> bool:
    """Run one stage. Returns True if the nudge should run again later.

    Stage 1 is the resync (ask SnapTrade to fetch from the broker); stage 2+ are
    reads. Both are skipped silently when the bucket is empty — the 30s sweep is
    always the backstop, so dropping a nudge costs latency, never correctness.
    """
    from app.services import snaptrade_listener as sl  # noqa: PLC0415

    if nudge.adapter is None:
        nudge.adapter = _adapter_for(nudge)
    adapter = nudge.adapter
    if adapter is None:
        return False

    if not nudge.resync_done:
        nudge.resync_done = True
        nudge.due_at = time.monotonic() + READ_DELAY_S
        last = _last_resync.get(nudge.account_id)
        recently = last is not None and (time.monotonic() - last) < RESYNC_MIN_INTERVAL_S
        # _refresh_unsupported is the listener's memo of connections whose
        # SnapTrade plan forbids manual refresh (403 / code 1141). Shared
        # deliberately: those plans serve real-time data, so retrying the
        # refresh would only burn 403s — but the READ below is still worth
        # doing, since real-time means the fill IS already there.
        unsupported = nudge.account_id in sl._refresh_unsupported  # noqa: SLF001
        if not recently and not unsupported and _take_token():
            _last_resync[nudge.account_id] = time.monotonic()
            if adapter.force_resync() == "forbidden":
                sl._refresh_unsupported.add(nudge.account_id)  # noqa: SLF001
        return True

    if nudge.reads_left <= 0 or not _take_token():
        return False
    nudge.reads_left -= 1
    attempt = MAX_READS - nudge.reads_left
    sl.reconcile_subscriber_account(
        nudge.user_id, nudge.account_id, adapter=adapter, light=True
    )
    settled = not _has_working_mirror(nudge)
    # One line per read, so the effect is measurable on prod without guessing:
    # grep for "settled on read 1" to count fills the nudge caught that would
    # otherwise have waited on SnapTrade's own cadence.
    log.info(
        "snaptrade nudge: account %s %s on read %d/%d",
        nudge.account_id,
        "settled" if settled else "still working",
        attempt, MAX_READS,
    )
    if nudge.reads_left <= 0 or settled:
        return False
    nudge.due_at = time.monotonic() + READ_RETRY_S
    return True


def _adapter_for(nudge: _Nudge) -> "object | None":
    """Build the adapter, confirming the account is a connected SnapTrade one.
    Callers of schedule() don't filter by broker, so this is the gate."""
    from app.brokers.snaptrade import SnapTradeAdapter  # noqa: PLC0415
    from app.database import SessionLocal  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount, BrokerName  # noqa: PLC0415
    from app.services import snaptrade_listener as sl  # noqa: PLC0415

    with SessionLocal() as db:
        acct = db.get(BrokerAccount, nudge.account_id)
        if (
            acct is None
            or acct.broker != BrokerName.SNAPTRADE
            or acct.connection_status != "connected"
        ):
            return None
    creds = sl._load_creds(nudge.user_id, nudge.account_id)  # noqa: SLF001
    return None if creds is None else SnapTradeAdapter(creds)


def _has_working_mirror(nudge: _Nudge) -> bool:
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import SessionLocal  # noqa: PLC0415
    from app.models.order import Order  # noqa: PLC0415
    from app.services import snaptrade_listener as sl  # noqa: PLC0415

    with SessionLocal() as db:
        return db.execute(
            select(Order.id).where(
                Order.user_id == nudge.user_id,
                Order.broker_account_id == nudge.account_id,
                Order.status.in_(sl._WORKING_STATUSES),  # noqa: SLF001
                Order.broker_order_id.is_not(None),
            ).limit(1)
        ).first() is not None


def _take_token() -> bool:
    with _cv:
        ok = _bucket.take()
    if not ok:
        log.warning(
            "snaptrade nudge: call budget exhausted (%.0f/min) — falling back "
            "to the 30s sweep for this order", _BUDGET_PER_MIN,
        )
    return ok


# ── Test / shutdown helpers ─────────────────────────────────────────────────

def set_enabled(value: bool) -> None:
    """Turn nudging off (tests, or a kill-switch if SnapTrade quota tightens)."""
    global _enabled
    _enabled = value


def drain(timeout: float = 30.0) -> bool:
    """Block until the queue empties. Tests only — never call from app code."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _cv:
            if not _pending and not _inflight:
                return True
        time.sleep(0.05)
    return False
