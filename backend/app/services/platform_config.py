"""Runtime-overridable platform config values, stored in Redis.

Pattern: each tunable has an env-var default (in ``app.config.Settings``) and an
optional Redis override. Admin endpoints set / clear the Redis key; runtime
code calls the getter below which reads Redis first, falls back to env.

Why Redis (not the DB):
- The values are read on every fanout — DB query on the critical path adds latency
- They change rarely (admin tweaks them); Redis GET is ~0.5ms
- No migration churn for adding a new tunable

Currently exposed:
- ``fanout_batch_threshold`` — see app.services.copy_engine
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)

# Redis key namespace. Keep the prefix stable so future tunables can co-locate.
_K_FANOUT_BATCH_THRESHOLD = "config:fanout_batch_threshold"
_K_ALPACA_PNL_POLL_INTERVAL = "config:alpaca_pnl_poll_interval_s"

# Inclusive bounds on the Alpaca poll interval. 1s is the floor — below
# that and we're spending more on outer-loop coordination than on real
# broker work. Even at 1s you only burn 1-2 req/sec/account against
# Alpaca's 3.3 req/sec/account budget, so quota-wise it's safe.
# 300s = 5 min is the ceiling so kill-switch latency never gets
# shockingly large.
_ALPACA_PNL_POLL_MIN_S = 1
_ALPACA_PNL_POLL_MAX_S = 300


def get_fanout_batch_threshold_sync() -> int:
    """Sync getter for callers outside an async context (admin handlers,
    initialization code). Falls back to env default if Redis is down or the
    override is missing/malformed."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = get_sync_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", _K_FANOUT_BATCH_THRESHOLD)
        return env_default
    return _coerce_threshold(raw, env_default)


async def get_fanout_batch_threshold_async() -> int:
    """Async getter used by copy_engine on the fanout hot path. Same
    fall-back semantics as the sync variant."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = await get_async_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", _K_FANOUT_BATCH_THRESHOLD)
        return env_default
    return _coerce_threshold(raw, env_default)


def set_fanout_batch_threshold(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError for non-positive ints
    so we never end up with a nonsensical 0/negative threshold."""
    r = get_sync_redis()
    if value is None:
        r.delete(_K_FANOUT_BATCH_THRESHOLD)
        return
    if value < 1:
        raise ValueError("fanout_batch_threshold must be >= 1")
    r.set(_K_FANOUT_BATCH_THRESHOLD, str(value))


def get_fanout_batch_threshold_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint — returns effective + default + whether
    an override is currently set. UI uses this to show 'Default' vs
    'Override' badges."""
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = get_sync_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        raw = None
    override = _coerce_optional(raw)
    return {
        "default": env_default,
        "override": override,
        "effective": override if override is not None else env_default,
    }


def _coerce_threshold(raw: object | None, fallback: int) -> int:
    """Parse a Redis value into a positive int, fall back on any error."""
    if raw is None:
        return fallback
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
        return v if v >= 1 else fallback
    except (TypeError, ValueError):
        return fallback


def _coerce_optional(raw: object | None) -> int | None:
    """Parse to int or return None if missing/malformed. Used by the GET
    endpoint to surface 'override is unset' truthfully."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
        return v if v >= 1 else None
    except (TypeError, ValueError):
        return None


# ── Alpaca P&L poll interval ────────────────────────────────────────────
# Same env-default + Redis-override pattern as fanout_batch_threshold.
# pnl_poller reads this on each tick (see _alpaca_interval_s) so an
# admin change takes effect on the very next tick — no restart needed.


def get_alpaca_pnl_poll_interval_sync() -> int:
    """Sync getter used by pnl_poller and the admin GET endpoint.
    Always returns a value inside [_ALPACA_PNL_POLL_MIN_S,
    _ALPACA_PNL_POLL_MAX_S]; out-of-range or malformed Redis values
    fall back to env default."""
    env_default = get_settings().alpaca_pnl_poll_interval_s
    try:
        raw = get_sync_redis().get(_K_ALPACA_PNL_POLL_INTERVAL)
    except Exception:  # noqa: BLE001
        log.warning(
            "redis get failed for %s — falling back to env default",
            _K_ALPACA_PNL_POLL_INTERVAL,
        )
        return _clamp_alpaca_interval(env_default, env_default)
    return _clamp_alpaca_interval(raw, env_default)


def set_alpaca_pnl_poll_interval(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError if outside the
    [5, 300] bound — protects against a misclicked extra zero pinning
    the poller at hours-per-tick."""
    r = get_sync_redis()
    if value is None:
        r.delete(_K_ALPACA_PNL_POLL_INTERVAL)
        return
    if value < _ALPACA_PNL_POLL_MIN_S or value > _ALPACA_PNL_POLL_MAX_S:
        raise ValueError(
            f"alpaca_pnl_poll_interval_s must be between "
            f"{_ALPACA_PNL_POLL_MIN_S} and {_ALPACA_PNL_POLL_MAX_S}"
        )
    r.set(_K_ALPACA_PNL_POLL_INTERVAL, str(value))


def get_alpaca_pnl_poll_interval_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint. ``override`` is None when no Redis
    key is set OR when the stored value is out-of-bounds (in which case
    the poller is silently using the env default and the UI should
    reflect that)."""
    env_default = get_settings().alpaca_pnl_poll_interval_s
    try:
        raw = get_sync_redis().get(_K_ALPACA_PNL_POLL_INTERVAL)
    except Exception:  # noqa: BLE001
        raw = None
    override = _coerce_optional_in_range(
        raw, _ALPACA_PNL_POLL_MIN_S, _ALPACA_PNL_POLL_MAX_S,
    )
    return {
        "default": env_default,
        "override": override,
        "effective": override if override is not None else env_default,
        "min": _ALPACA_PNL_POLL_MIN_S,
        "max": _ALPACA_PNL_POLL_MAX_S,
    }


def _clamp_alpaca_interval(raw: object | None, fallback: int) -> int:
    """Like _coerce_threshold but also clamps to [min, max]. The poller
    is the hot-path caller — we don't want to keep hitting Redis with a
    parsed-but-invalid value, so we silently clamp instead of erroring."""
    if raw is None:
        return fallback
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    if v < _ALPACA_PNL_POLL_MIN_S or v > _ALPACA_PNL_POLL_MAX_S:
        return fallback
    return v


def _coerce_optional_in_range(
    raw: object | None, lo: int, hi: int,
) -> int | None:
    """Same shape as _coerce_optional but rejects out-of-bound values
    so the admin UI's 'Override: -' renders truthfully."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


# ─── Generic polling knobs ──────────────────────────────────────────────
# The two tunables above each got their own getter/setter/state trio. That
# does not scale past a handful, so the polling knobs the admin API page
# exposes are declared as data here and served by one generic trio.
#
# Each knob is read on its loop's hot path, so a change lands on the next
# tick with no restart. Every read falls back to the env default when Redis
# is unreachable or the stored value is unparseable / out of bounds — a dead
# cache tier must never be able to stop a poller or pin it at a silly value.
#
# The bounds are not cosmetic: they encode real broker limits, and they are
# enforced in the SETTER (422 to the admin) as well as clamped in the getter.
# See each knob's `why` for what the floor is protecting.

from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class IntKnob:
    """An integer-seconds knob. ``setting`` names the Settings attribute
    holding the env default; ``lo``/``hi`` are inclusive."""

    name: str
    setting: str
    lo: int
    hi: int
    group: str
    label: str
    why: str
    # "seconds" → formatted as a duration, unit picker offered.
    # "count" / "rate" → a plain number with `unit_label` as its suffix.
    kind: str = "seconds"
    unit_label: str = "sec"
    # Matrix coordinates. `broker` is None for a loop that is not per-broker
    # (one sweep covers every connected account); `row` is the setting kind,
    # shared across brokers so a row lines up.
    broker: str | None = None
    row: str = ""

    @property
    def key(self) -> str:
        return f"config:{self.name}"


@dataclass(frozen=True)
class BoolKnob:
    """An on/off knob. Stored as "1"/"0"; absent means "use the env default"."""

    name: str
    setting: str
    group: str
    label: str
    why: str
    broker: str | None = None
    row: str = ""

    @property
    def key(self) -> str:
        return f"config:{self.name}"


# The groups the admin page renders, in order. Named for what an operator is
# actually trying to change, not for which module owns the loop.
GROUP_PNL = "P&L"
GROUP_ORDERS = "Order feed & status"
GROUP_BALANCE = "Account balance"
GROUP_POSITIONS = "Positions"
GROUP_FILL = "Order fill"
GROUP_CALENDAR = "Broker calendar"

GROUP_ORDER: tuple[str, ...] = (
    GROUP_ORDERS, GROUP_FILL, GROUP_PNL,
    GROUP_POSITIONS, GROUP_BALANCE, GROUP_CALENDAR,
)

INT_KNOBS: tuple[IntKnob, ...] = (
    # ── Order feed & status ─────────────────────────────────────────────
    IntKnob(
        name="snaptrade_poll_interval_s",
        setting="snaptrade_poll_interval_s",
        broker="snaptrade", row="Trader order feed",
        lo=5, hi=120, group=GROUP_ORDERS,
        label="SnapTrade · order feed",
        why=(
            "The largest single consumer of the shared SnapTrade quota: 12 "
            "req/min per connected trader at 5s. Floored at 5s so it can only "
            "be relaxed from where it is today. Ignored entirely while the "
            "SnapTrade webhook switch is on (the listener then runs at its 60s "
            "backstop)."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_seconds",
        setting="webull_poll_interval_seconds",
        broker="webull", row="Trader order feed",
        lo=5, hi=300, group=GROUP_ORDERS,
        label="Webull · order list poll (market open)",
        why=(
            "Runs ALONGSIDE the gRPC stream, permanently — it is a second "
            "detection channel, not a fallback. The listener floors the "
            "effective value at 1.2s × accounts and spaces the per-account "
            "calls across the cycle, so this is a target rather than a "
            "guarantee. Turn the poll off entirely with the switch below."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_closed_seconds",
        setting="webull_poll_interval_closed_seconds",
        broker="webull", row="Trader order feed · market closed",
        lo=5, hi=600, group=GROUP_ORDERS,
        label="Webull · order list poll (market closed)",
        why=(
            "Overnight and weekend cadence, used when the trader has NO working "
            "orders. Nothing can fill while the market is shut, so this exists "
            "only to notice a cancel or an external change."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_closed_active_seconds",
        setting="webull_poll_interval_closed_active_seconds",
        broker="webull", row="Trader order feed · closed, orders working",
        lo=5, hi=600, group=GROUP_ORDERS,
        label="Webull · order list poll (closed, orders working)",
        why=(
            "Faster closed-market cadence used while the trader still has "
            "working orders, since those can be cancelled or amended "
            "out-of-hours. Floored at 5s."
        ),
    ),
    IntKnob(
        name="ibkr_order_poll_interval_s",
        setting="ibkr_order_poll_interval_s",
        broker="ibkr", row="Trader order feed",
        lo=2, hi=60, group=GROUP_ORDERS,
        label="IBKR · order poll",
        why=(
            "IBKR has no order stream, so this poll is the ONLY way a trader's "
            "fills are detected. Every call shares the user's single brokerage "
            "session, so a tight cadence competes with order placement rather "
            "than with a published quota."
        ),
    ),
    IntKnob(
        name="alpaca_subscriber_reconcile_interval_s",
        setting="alpaca_subscriber_reconcile_interval_s",
        broker="alpaca", row="Subscriber order status",
        lo=10, hi=300, group=GROUP_ORDERS,
        label="Alpaca · subscriber order status",
        why=(
            "Subscriber mirror accounts get no stream, so without this a mirror "
            "that fills stays WORKING in our DB — which breaks close detection "
            "and order history, not just the display. One get_order per "
            "unfilled order per pass."
        ),
    ),
    IntKnob(
        name="snaptrade_subscriber_reconcile_interval_s",
        setting="snaptrade_subscriber_reconcile_interval_s",
        broker="snaptrade", row="Subscriber order status",
        lo=10, hi=300, group=GROUP_ORDERS,
        label="SnapTrade · subscriber order status",
        why=(
            "Same job as the Alpaca one, but each pass costs one get_order per "
            "unfilled order PLUS a positions read, against a quota shared by "
            "the whole platform. This is the knob that makes stuck working "
            "orders expensive."
        ),
    ),
    IntKnob(
        name="webull_subscriber_sync_interval_s",
        setting="webull_subscriber_sync_interval_s",
        broker="webull", row="Subscriber order status",
        lo=2, hi=120, group=GROUP_ORDERS,
        label="Webull · subscriber order status (just traded)",
        why=(
            "Fast cadence used for an account with order activity inside the "
            "90s window. Inert unless direct-Webull execution is enabled."
        ),
    ),
    IntKnob(
        name="webull_subscriber_sync_idle_interval_s",
        setting="webull_subscriber_sync_idle_interval_s",
        broker="webull", row="Subscriber order status · idle",
        lo=10, hi=600, group=GROUP_ORDERS,
        label="Webull · subscriber order status (idle)",
        why="Cadence for a Webull subscriber account with nothing recent.",
    ),
    IntKnob(
        name="ibkr_subscriber_sync_interval_s",
        setting="ibkr_subscriber_sync_interval_s",
        broker="ibkr", row="Subscriber order status",
        lo=2, hi=120, group=GROUP_ORDERS,
        label="IBKR · subscriber order status (just traded)",
        why=(
            "Fast cadence for a recently-active IBKR subscriber account. This "
            "is the only thing that moves an IBKR mirror from SUBMITTED to "
            "FILLED or CANCELED."
        ),
    ),
    IntKnob(
        name="ibkr_subscriber_sync_idle_interval_s",
        setting="ibkr_subscriber_sync_idle_interval_s",
        broker="ibkr", row="Subscriber order status · idle",
        lo=10, hi=600, group=GROUP_ORDERS,
        label="IBKR · subscriber order status (idle)",
        why="Cadence for an IBKR subscriber account with nothing recent.",
    ),
    # ── Order fill ──────────────────────────────────────────────────────
    IntKnob(
        name="snaptrade_nudge_read_delay_s",
        setting="snaptrade_nudge_read_delay_s",
        broker="snaptrade", row="Fill nudge · first read",
        lo=1, hi=60, group=GROUP_FILL,
        label="SnapTrade fill nudge · first read",
        why=(
            "SnapTrade serves cached brokerage data, so a mirror can be filled "
            "long before our reads can see it. force_resync() fires the moment "
            "we place; this is how long we then wait before reading back. Too "
            "short and the resync hasn't landed; too long and the 30s sweep "
            "would have caught it anyway."
        ),
    ),
    IntKnob(
        name="snaptrade_nudge_retry_delay_s",
        setting="snaptrade_nudge_retry_delay_s",
        broker="snaptrade", row="Fill nudge · retry gap",
        lo=1, hi=120, group=GROUP_FILL,
        label="SnapTrade fill nudge · retry gap",
        why="Wait before the second read, when the first found nothing settled.",
    ),
    IntKnob(
        name="snaptrade_nudge_max_reads",
        setting="snaptrade_nudge_max_reads",
        broker="snaptrade", row="Fill nudge · reads per placement",
        lo=1, hi=5, group=GROUP_FILL,
        kind="count", unit_label="reads",
        label="SnapTrade fill nudge · reads per placement",
        why=(
            "How many read-backs one placement may cost. Counted in REQUESTS, "
            "not seconds — raising it multiplies nudge spend by every mirror "
            "placed, which on a wide fanout is the burstiest thing we do to "
            "SnapTrade."
        ),
    ),
    IntKnob(
        name="snaptrade_nudge_budget_per_min",
        setting="snaptrade_nudge_budget_per_min",
        broker="snaptrade", row="Fill nudge · request budget",
        lo=10, hi=250, group=GROUP_FILL,
        kind="rate", unit_label="req/min",
        label="SnapTrade fill nudge · request budget",
        why=(
            "Hard ceiling on nudge requests per minute, independent of how many "
            "orders are placed. This is the circuit breaker that stops a large "
            "fanout's nudges starving the order listener of quota — lower it if "
            "SnapTrade ever gets tight."
        ),
    ),
    # ── P&L ─────────────────────────────────────────────────────────────
    IntKnob(
        name="alpaca_pnl_poll_interval_s",
        setting="alpaca_pnl_poll_interval_s",
        broker="alpaca", row="P&L poll",
        lo=_ALPACA_PNL_POLL_MIN_S, hi=_ALPACA_PNL_POLL_MAX_S,
        group=GROUP_PNL,
        label="Alpaca · P&L poll",
        why=(
            "200 req/min per account key, so there is ample headroom. Note this "
            "knob ALSO sets the browser's positions-page refresh rate via "
            "brokers.capabilities.effective_day_pnl_interval_s — lowering it "
            "speeds up the page as well as the worker."
        ),
    ),
    IntKnob(
        name="webull_pnl_poll_interval_s",
        setting="webull_pnl_poll_interval_s",
        broker="webull", row="P&L poll",
        lo=5, hi=300, group=GROUP_PNL,
        label="Webull · P&L poll",
        why=(
            "Reads cost 2 per 2s per endpoint (60/min). Volume is not the "
            "constraint — bunching is. Floored at 5s because the risk-tick "
            "sub-enforcers already share one read per tick and a tighter "
            "cadence starts colliding with the positions page."
        ),
    ),
    IntKnob(
        name="snaptrade_pnl_poll_interval_s",
        setting="snaptrade_pnl_poll_interval_s",
        broker="snaptrade", row="P&L poll",
        lo=30, hi=600, group=GROUP_PNL,
        label="SnapTrade · P&L poll",
        why=(
            "SnapTrade's 250 req/min is shared across the WHOLE platform, so "
            "this is the most expensive knob here: halving it doubles the spend "
            "for every user at once. Floored at 30s."
        ),
    ),
    IntKnob(
        name="ibkr_pnl_poll_interval_s",
        setting="ibkr_pnl_poll_interval_s",
        broker="ibkr", row="P&L poll",
        lo=5, hi=300, group=GROUP_PNL,
        label="IBKR · P&L poll",
        why=(
            "Generous rate budget, but every call shares the user's single "
            "brokerage session with the IBKR order poll, so a tight cadence "
            "competes with order detection rather than with a quota."
        ),
    ),
    # ── Positions ───────────────────────────────────────────────────────
    IntKnob(
        name="webull_positions_cache_ttl_s",
        setting="webull_positions_cache_ttl_s",
        broker="webull", row="Positions read cache",
        lo=1, hi=30, group=GROUP_POSITIONS,
        label="Webull · positions read cache",
        why=(
            "DISPLAY reads inside this window share one answer; the risk path "
            "(liquidation, TP/SL, retry) always reads live and is unaffected. "
            "Raising it is the most direct fix for the positions page 429ing "
            "under the post-order refresh burst — at the cost of showing a "
            "position that was just closed for up to this long."
        ),
    ),
    IntKnob(
        name="close_reconcile_interval_s",
        setting="close_reconcile_interval_s",
        broker=None, row="Close reconciler sweep",
        lo=10, hi=600, group=GROUP_POSITIONS,
        label="Close reconciler · positions sweep",
        why=(
            "Reads each subscriber's positions to find ones the trader has "
            "already exited but the subscriber still holds. One positions read "
            "per subscriber per pass. Inert unless close_reconcile_enabled is "
            "on, which it is not by default."
        ),
    ),
    # ── Broker calendar ─────────────────────────────────────────────────
    IntKnob(
        name="pnl_snapshot_interval_s",
        setting="pnl_snapshot_interval_s",
        broker=None, row="Calendar realized-P&L snapshot",
        lo=300, hi=86400, group=GROUP_CALENDAR,
        label="Calendar · realized P&L snapshot",
        why=(
            "The sweep that stores broker-direct realized P&L for past days, "
            "which is what the Calendar shows. Cheap (hourly), and if it stops "
            "running the Calendar silently falls back to an approximate value "
            "derived from our own rows."
        ),
    ),
    IntKnob(
        name="day_pnl_share_s",
        setting="day_pnl_share_s",
        broker=None, row="Day P&L shared-result window",
        lo=1, hi=120, group=GROUP_CALENDAR,
        label="Day P&L · shared-result window",
        why=(
            "One computed account Day P&L is reused for this long across every "
            "tab and every process. It is the only thing stopping N open tabs "
            "becoming N broker calls, so lowering it multiplies page-driven "
            "load directly. Note the Calendar's own today figure does NOT go "
            "through this cache."
        ),
    ),
    # ── Account balance ─────────────────────────────────────────────────
    IntKnob(
        name="balance_sync_interval_s",
        setting="balance_sync_interval_s",
        broker=None, row="Balance sweep (background)",
        lo=60, hi=7200, group=GROUP_BALANCE,
        label="Balance sweep (background)",
        why=(
            "Keeps stored equity current for accounts nobody has open. One "
            "get_account per connected account per pass, across all brokers."
        ),
    ),
    IntKnob(
        name="broker_balance_stale_s",
        setting="broker_balance_stale_s",
        broker=None, row="Balance freshness on page load",
        lo=30, hi=3600, group=GROUP_BALANCE,
        label="Balance freshness on page load",
        why=(
            "GET /api/brokers refreshes any balance older than this INLINE, so "
            "with a page open this — not the sweep above — is the real balance "
            "cadence. Every Brokers, Positions and Calendar page load consults "
            "it, so raising it cuts calls on a per-page-load basis."
        ),
    ),
)

BOOL_KNOBS: tuple[BoolKnob, ...] = (
    BoolKnob(
        name="snaptrade_webhook_enabled",
        setting="snaptrade_webhook_enabled",
        broker="snaptrade", row="Trust webhook (poll backs off to 60s)",
        group=GROUP_ORDERS,
        label="SnapTrade · trust the webhook",
        why=(
            "On = assume SnapTrade's webhook delivers fills, so the order "
            "listener drops from its poll interval to a 60s backstop. This is "
            "the single biggest available reduction in SnapTrade spend (12 "
            "req/min → 1 per trader) and costs nothing in detection latency "
            "IF the webhook is actually arriving. Turn it on only once you have "
            "confirmed deliveries."
        ),
    ),
    BoolKnob(
        name="webull_direct_poll_enabled",
        setting="webull_direct_poll_enabled",
        broker="webull", row="Order poll on/off",
        group=GROUP_ORDERS,
        label="Webull · order list poll",
        why=(
            "Webull detects orders on two channels at once: the gRPC stream and "
            "this REST poll. Off = the stream becomes the SOLE path to detecting "
            "a trader's fills. The stream was silent for two months before the "
            "subscribeType fix and this poll is what carried detection through "
            "it, so turn it off only with stream evidence in hand."
        ),
    ),
)

BROKER_ORDER: tuple[str, ...] = ("alpaca", "webull", "snaptrade", "ibkr")

BROKER_LABELS: dict[str, str] = {
    "alpaca": "Alpaca",
    "webull": "Webull",
    "snaptrade": "SnapTrade",
    "ibkr": "IBKR",
}


# Why a given cell is empty, where a bare dash would read as "broken" rather
# than "not applicable". Only the cases a reader actively wonders about — the
# rest are self-evident from the row name.
NA_NOTES: dict[tuple[str, str], str] = {
    ("alpaca", "Trader order feed"):
        "Alpaca pushes order events over a WebSocket — there is no poll to tune.",
    ("alpaca", "Subscriber order status · idle"):
        "Alpaca's subscriber reconciler runs at one flat interval, with no "
        "separate idle cadence.",
    ("snaptrade", "Subscriber order status · idle"):
        "SnapTrade's subscriber reconciler runs at one flat interval, with no "
        "separate idle cadence.",
    ("alpaca", "Positions read cache"):
        "Only the Webull adapter caches display reads; Alpaca reads live.",
    ("snaptrade", "Positions read cache"):
        "Only the Webull adapter caches display reads; SnapTrade reads live.",
    ("ibkr", "Positions read cache"):
        "Only the Webull adapter caches display reads; IBKR reads live.",
}


def _matrix_rows() -> list[dict]:
    """Distinct (group, row) pairs for the per-broker matrix.

    Ordered by GROUP_ORDER, then switches before intervals inside a group — a
    toggle decides whether the intervals under it matter at all, so it reads
    better above them. Within those, registry order, so a row's position stays
    editorial rather than alphabetical."""
    out: list[dict] = []
    for group in GROUP_ORDER:
        seen: list[str] = []
        for knob in list(BOOL_KNOBS) + list(INT_KNOBS):
            if knob.broker is None or knob.group != group:
                continue
            if knob.row not in seen:
                seen.append(knob.row)
        out.extend({"group": group, "row": r} for r in seen)
    return out


_INT_BY_NAME: dict[str, IntKnob] = {k.name: k for k in INT_KNOBS}
_BOOL_BY_NAME: dict[str, BoolKnob] = {k.name: k for k in BOOL_KNOBS}


def _env_int(knob: IntKnob) -> int:
    return int(getattr(get_settings(), knob.setting))


def get_int_knob(name: str) -> int:
    """Effective value for ``name``, clamped into the knob's bounds. Hot-path
    caller — never raises, never blocks on a dead Redis."""
    knob = _INT_BY_NAME[name]
    env_default = _env_int(knob)
    try:
        raw = get_sync_redis().get(knob.key)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", knob.key)
        return env_default
    return _clamp_in_range(raw, env_default, knob.lo, knob.hi)


def int_knob_or(name: str, fallback: float) -> float:
    """``get_int_knob`` for poll loops: returns ``fallback`` instead of raising
    if anything at all goes wrong. Every caller is on a hot path where an
    exception would take a poller down, and the fallback is the constant the
    loop used before it was tunable."""
    try:
        return float(get_int_knob(name))
    except Exception:  # noqa: BLE001
        log.warning("knob %s unreadable — using fallback %s", name, fallback)
        return float(fallback)


def bool_knob_or(name: str, fallback: bool) -> bool:
    """``get_bool_knob`` with the same never-raise contract."""
    try:
        return get_bool_knob(name)
    except Exception:  # noqa: BLE001
        log.warning("knob %s unreadable — using fallback %s", name, fallback)
        return bool(fallback)


def set_int_knob(name: str, value: int | None) -> None:
    """Admin write. ``value=None`` clears the override. Raises ValueError for
    out-of-bounds, which the endpoint turns into a 422 — a misclicked extra
    zero must not silently pin a poller at hours per tick."""
    knob = _INT_BY_NAME[name]
    r = get_sync_redis()
    if value is None:
        r.delete(knob.key)
        return
    if value < knob.lo or value > knob.hi:
        raise ValueError(f"{knob.name} must be between {knob.lo} and {knob.hi}")
    r.set(knob.key, str(value))


def get_bool_knob(name: str) -> bool:
    """Effective value for a switch. Same fallback contract as get_int_knob."""
    knob = _BOOL_BY_NAME[name]
    env_default = bool(getattr(get_settings(), knob.setting))
    try:
        raw = get_sync_redis().get(knob.key)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", knob.key)
        return env_default
    parsed = _coerce_optional_bool(raw)
    return env_default if parsed is None else parsed


def set_bool_knob(name: str, value: bool | None) -> None:
    """Admin write. ``value=None`` clears the override."""
    knob = _BOOL_BY_NAME[name]
    r = get_sync_redis()
    if value is None:
        r.delete(knob.key)
        return
    r.set(knob.key, "1" if value else "0")


def get_polling_knobs_state() -> dict[str, list[dict]]:
    """Everything the admin page needs for one render: per knob the default,
    the override (None when unset OR stored out-of-bounds, so the UI's '-'
    is truthful), the effective value, and the bounds for the input."""
    ints: list[dict] = []
    for knob in INT_KNOBS:
        env_default = _env_int(knob)
        try:
            raw = get_sync_redis().get(knob.key)
        except Exception:  # noqa: BLE001
            raw = None
        override = _coerce_optional_in_range(raw, knob.lo, knob.hi)
        ints.append({
            "name": knob.name,
            "group": knob.group,
            "broker": knob.broker,
            "row": knob.row,
            "kind": knob.kind,
            "unit_label": knob.unit_label,
            "label": knob.label,
            "why": knob.why,
            "default": env_default,
            "override": override,
            "effective": override if override is not None else env_default,
            "min": knob.lo,
            "max": knob.hi,
        })

    bools: list[dict] = []
    for bknob in BOOL_KNOBS:
        env_default = bool(getattr(get_settings(), bknob.setting))
        try:
            raw = get_sync_redis().get(bknob.key)
        except Exception:  # noqa: BLE001
            raw = None
        override = _coerce_optional_bool(raw)
        bools.append({
            "name": bknob.name,
            "group": bknob.group,
            "broker": bknob.broker,
            "row": bknob.row,
            "label": bknob.label,
            "why": bknob.why,
            "default": env_default,
            "override": override,
            "effective": env_default if override is None else override,
        })

    return {
        "intervals": ints,
        "switches": bools,
        "groups": list(GROUP_ORDER),
        # The grid's axes, so the page never hardcodes a broker list or a row
        # order that can drift from the registry.
        "brokers": [
            {"name": b, "label": BROKER_LABELS[b]} for b in BROKER_ORDER
        ],
        "matrix_rows": _matrix_rows(),
        "na_notes": [
            {"broker": b, "row": r, "note": note}
            for (b, r), note in NA_NOTES.items()
        ],
    }


def _clamp_in_range(raw: object | None, fallback: int, lo: int, hi: int) -> int:
    """Parse and bound-check, returning ``fallback`` for anything unusable.
    Silently falls back rather than raising: the callers are poll loops, and a
    bad Redis value must not take a poller down."""
    if raw is None:
        return fallback
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return v if lo <= v <= hi else fallback


def _coerce_optional_bool(raw: object | None) -> bool | None:
    """None when no override is stored or the stored value is not recognisable."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    s = str(raw).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None
