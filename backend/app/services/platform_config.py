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


# ── the durable store ────────────────────────────────────────────────────────
#
# Redis is a CACHE for these overrides, never the source of truth. QA runs it
# as `--save "" --appendonly no --maxmemory-policy allkeys-lru`, so a restart
# or memory pressure drops every key — and because a miss was indistinguishable
# from "no override set", an admin's setting silently reverted to the env
# default and the page then showed "-" as though nobody had ever set one.
#
# The row in ``app_settings`` is what survives. Reads go cache → Redis → DB →
# env default, each layer bounding the one below: the in-process TTL keeps a
# dead Redis from turning a 1-per-second hot path into a database read per
# tick. Writes go to the DB first, because that is the copy that matters.

_DB_CACHE_TTL_S = 10.0
# key -> (expires_at, raw stored value or None). None is cached too: "no
# override" is an answer, and re-asking the database every tick for it is the
# cost this layer exists to avoid.
_db_cache: dict[str, tuple[float, str | None]] = {}


def _db_get(key: str) -> str | None:
    """The stored override for ``key``, or None. Never raises: a missing table
    (pre-migration) or a transient database error reads as "no override", which
    is the same answer this module gave before it had a database at all."""
    import time  # noqa: PLC0415

    hit = _db_cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]
    raw: str | None = None
    try:
        from sqlalchemy import select  # noqa: PLC0415

        from app.database import SessionLocal  # noqa: PLC0415
        from app.models.app_setting import AppSetting  # noqa: PLC0415

        with SessionLocal() as db:
            raw = db.execute(
                select(AppSetting.value).where(AppSetting.key == key)
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        log.warning("platform_config: db read failed for %s — treating as unset", key)
        raw = None
    _db_cache[key] = (now + _DB_CACHE_TTL_S, raw)
    return raw


def _db_set(key: str, value: str | None) -> None:
    """Upsert or delete the durable override and bust the in-process cache.

    Raises on failure, deliberately: an admin write that did not persist must
    not report success, or we are back to settings that quietly disappear.
    """
    from app.database import SessionLocal  # noqa: PLC0415
    from app.models.app_setting import AppSetting  # noqa: PLC0415

    with SessionLocal() as db:
        row = db.get(AppSetting, key)
        if value is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        db.commit()
    _db_cache.pop(key, None)


def _stored(key: str) -> str | None:
    """The raw override behind ``key``: Redis if it has it, else the database.

    A database hit warms Redis so the next tick is cheap again. A Redis miss is
    no longer an answer on its own — that conflation is what let a restart wipe
    the configuration.
    """
    try:
        raw = get_sync_redis().get(key)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — reading the database instead", key)
        raw = None
    if raw is not None:
        return raw if isinstance(raw, str) else raw.decode()
    stored = _db_get(key)
    if stored is not None:
        try:
            get_sync_redis().set(key, stored)
        except Exception:  # noqa: BLE001
            pass      # the value still stands; only the cache went unwarmed
    return stored


def _write_through(key: str, value: str | None) -> None:
    """Durable first, then the cache. If the database write fails the caller
    sees the exception and Redis is left untouched, so the two never disagree
    about a value the admin was told was saved."""
    _db_set(key, value)
    try:
        r = get_sync_redis()
        if value is None:
            r.delete(key)
        else:
            r.set(key, value)
    except Exception:  # noqa: BLE001
        log.warning("platform_config: %s saved, but the cache was not updated", key)


def backfill_overrides_from_cache() -> int:
    """Copy any override that exists only in Redis into the database. Returns
    how many were carried over.

    Run once at startup: without it, the first deploy of DB-backed knobs looks
    exactly like the failure it fixes — every admin setting reset to its env
    default. Idempotent, and never overwrites a row that already exists.
    """
    carried = 0
    for key in [k.key for k in INT_KNOBS] + [k.key for k in BOOL_KNOBS] + [
        _K_ALPACA_PNL_POLL_INTERVAL, _K_FANOUT_BATCH_THRESHOLD,
    ]:
        try:
            raw = get_sync_redis().get(key)
        except Exception:  # noqa: BLE001
            return carried        # no cache to read; nothing to carry over
        if raw is None:
            continue
        if _db_get(key) is not None:
            continue
        try:
            _db_set(key, raw if isinstance(raw, str) else raw.decode())
            carried += 1
            log.info("platform_config: carried %s over to the database", key)
        except Exception:  # noqa: BLE001
            log.exception("platform_config: could not carry %s over", key)
    return carried


def get_fanout_batch_threshold_sync() -> int:
    """Sync getter for callers outside an async context (admin handlers,
    initialization code). Falls back to env default if Redis is down or the
    override is missing/malformed."""
    env_default = get_settings().fanout_batch_threshold
    return _coerce_threshold(_stored(_K_FANOUT_BATCH_THRESHOLD), env_default)


async def get_fanout_batch_threshold_async() -> int:
    """Async getter used by copy_engine on the fanout hot path.

    Reads the cache only. Unlike the sync accessors this does NOT fall through
    to the database: the database read is blocking, and this runs on the event
    loop while a fanout is in flight — one slow query would stall every
    subscriber's order, which is a far worse failure than briefly using the
    env default for a batching threshold.

    It stays correct in practice because the cache is kept warm from the other
    side: ``backfill_overrides_from_cache`` at startup, and every sync read
    re-warming Redis from the database. The window where this can disagree is
    between a cache loss and the next sync read, and the cost of being wrong is
    a batch size, not a wrong trade.
    """
    env_default = get_settings().fanout_batch_threshold
    try:
        raw = await get_async_redis().get(_K_FANOUT_BATCH_THRESHOLD)
    except Exception:  # noqa: BLE001
        log.warning("redis get failed for %s — falling back to env default", _K_FANOUT_BATCH_THRESHOLD)
        return env_default
    if raw is None:
        # Not a cache miss we can resolve here — see above. Use the in-process
        # copy if a sync reader already fetched it; never block to find out.
        hit = _db_cache.get(_K_FANOUT_BATCH_THRESHOLD)
        raw = hit[1] if hit is not None else None
    return _coerce_threshold(raw, env_default)


def set_fanout_batch_threshold(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError for non-positive ints
    so we never end up with a nonsensical 0/negative threshold."""
    if value is None:
        _write_through(_K_FANOUT_BATCH_THRESHOLD, None)
        return
    if value < 1:
        raise ValueError("fanout_batch_threshold must be >= 1")
    _write_through(_K_FANOUT_BATCH_THRESHOLD, str(value))


def get_fanout_batch_threshold_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint — returns effective + default + whether
    an override is currently set. UI uses this to show 'Default' vs
    'Override' badges."""
    env_default = get_settings().fanout_batch_threshold
    override = _coerce_optional(_stored(_K_FANOUT_BATCH_THRESHOLD))
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
    return _clamp_alpaca_interval(_stored(_K_ALPACA_PNL_POLL_INTERVAL), env_default)


def set_alpaca_pnl_poll_interval(value: int | None) -> None:
    """Admin write. ``value=None`` deletes the override (effective value
    becomes the env default again). Raises ValueError if outside the
    [5, 300] bound — protects against a misclicked extra zero pinning
    the poller at hours-per-tick."""
    if value is None:
        _write_through(_K_ALPACA_PNL_POLL_INTERVAL, None)
        return
    if value < _ALPACA_PNL_POLL_MIN_S or value > _ALPACA_PNL_POLL_MAX_S:
        raise ValueError(
            f"alpaca_pnl_poll_interval_s must be between "
            f"{_ALPACA_PNL_POLL_MIN_S} and {_ALPACA_PNL_POLL_MAX_S}"
        )
    _write_through(_K_ALPACA_PNL_POLL_INTERVAL, str(value))


def get_alpaca_pnl_poll_interval_state() -> dict[str, int | bool | None]:
    """For the admin GET endpoint. ``override`` is None when no Redis
    key is set OR when the stored value is out-of-bounds (in which case
    the poller is silently using the env default and the UI should
    reflect that)."""
    env_default = get_settings().alpaca_pnl_poll_interval_s
    raw = _stored(_K_ALPACA_PNL_POLL_INTERVAL)
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
            "How often we ask SnapTrade for the trader's latest orders. This is "
            "the single biggest user of our shared SnapTrade request allowance "
            "(about 12 requests a minute per trader at 5s). Lower = fills noticed "
            "sooner but more requests. Cannot go below 5s. Ignored while 'Trust "
            "webhook' is on."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_seconds",
        setting="webull_poll_interval_seconds",
        broker="webull", row="Trader order feed",
        lo=5, hi=300, group=GROUP_ORDERS,
        label="Webull · order list poll (market open)",
        why=(
            "How often we ask Webull for the trader's order list while the market "
            "is open. This runs in addition to Webull's live stream, as a second "
            "way to catch fills. With several Webull traders the real gap can be "
            "a little longer than set, because the calls are spread out so they "
            "don't bunch up. Use the 'Order poll on/off' switch to turn it off "
            "completely."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_closed_seconds",
        setting="webull_poll_interval_closed_seconds",
        broker="webull", row="Trader order feed · market closed",
        lo=5, hi=600, group=GROUP_ORDERS,
        label="Webull · order list poll (market closed)",
        why=(
            "How often we check the trader's Webull orders overnight and on "
            "weekends when they have no open orders. Nothing can fill while the "
            "market is closed, so this only needs to notice a cancel or a change "
            "made outside Kopyya. It can be slow."
        ),
    ),
    IntKnob(
        name="webull_poll_interval_closed_active_seconds",
        setting="webull_poll_interval_closed_active_seconds",
        broker="webull", row="Trader order feed · closed, orders working",
        lo=5, hi=600, group=GROUP_ORDERS,
        label="Webull · order list poll (closed, orders working)",
        why=(
            "How often we check the trader's Webull orders while the market is "
            "closed but they still have open orders. Those can still be cancelled "
            "or changed after hours, so this runs faster than the plain "
            "closed-market check. Cannot go below 5s."
        ),
    ),
    IntKnob(
        name="ibkr_order_poll_interval_s",
        setting="ibkr_order_poll_interval_s",
        broker="ibkr", row="Trader order feed",
        lo=2, hi=60, group=GROUP_ORDERS,
        label="IBKR · order poll",
        why=(
            "How often we ask IBKR for the trader's orders. IBKR has no live "
            "stream, so this is the only way a trader's fills are noticed. Lower "
            "= noticed sooner, but every call shares the trader's single IBKR "
            "session with order placement, so going too low can slow down placing "
            "orders."
        ),
    ),
    IntKnob(
        name="alpaca_subscriber_reconcile_interval_s",
        setting="alpaca_subscriber_reconcile_interval_s",
        broker="alpaca", row="Subscriber order status",
        lo=10, hi=300, group=GROUP_ORDERS,
        label="Alpaca · subscriber order status",
        why=(
            "How often we check Alpaca for each subscriber's unfilled mirror "
            "orders to see if they filled or were cancelled. Subscribers get no "
            "live stream, so without this a filled mirror would stay 'working' in "
            "Kopyya, which breaks close detection and order history. One request "
            "per unfilled order per check."
        ),
    ),
    IntKnob(
        name="snaptrade_subscriber_reconcile_interval_s",
        setting="snaptrade_subscriber_reconcile_interval_s",
        broker="snaptrade", row="Subscriber order status",
        lo=10, hi=300, group=GROUP_ORDERS,
        label="SnapTrade · subscriber order status",
        why=(
            "How often we check SnapTrade for each subscriber's unfilled mirror "
            "orders. Same job as the Alpaca one, but each check costs one request "
            "per unfilled order plus a positions read, taken from the SnapTrade "
            "allowance shared by everyone. Many stuck orders plus a fast interval "
            "means a lot of requests."
        ),
    ),
    IntKnob(
        name="webull_subscriber_sync_interval_s",
        setting="webull_subscriber_sync_interval_s",
        broker="webull", row="Subscriber order status",
        lo=2, hi=120, group=GROUP_ORDERS,
        label="Webull · subscriber order status (just traded)",
        why=(
            "How often we check a Webull subscriber's orders right after they "
            "traded (within the last 90 seconds). Fast, so a mirror shows as "
            "filled quickly. Only matters for subscribers who execute directly on "
            "Webull."
        ),
    ),
    IntKnob(
        name="webull_subscriber_sync_idle_interval_s",
        setting="webull_subscriber_sync_idle_interval_s",
        broker="webull", row="Subscriber order status · idle",
        lo=10, hi=600, group=GROUP_ORDERS,
        label="Webull · subscriber order status (idle)",
        why=(
            "How often we check a Webull subscriber's orders when nothing has "
            "happened recently. Slower, to save requests."
        ),
    ),
    IntKnob(
        name="ibkr_subscriber_sync_interval_s",
        setting="ibkr_subscriber_sync_interval_s",
        broker="ibkr", row="Subscriber order status",
        lo=2, hi=120, group=GROUP_ORDERS,
        label="IBKR · subscriber order status (just traded)",
        why=(
            "How often we check an IBKR subscriber's orders right after they "
            "traded. This is the only thing that moves an IBKR mirror from "
            "'submitted' to 'filled' or 'cancelled', so keep it fast."
        ),
    ),
    IntKnob(
        name="ibkr_subscriber_sync_idle_interval_s",
        setting="ibkr_subscriber_sync_idle_interval_s",
        broker="ibkr", row="Subscriber order status · idle",
        lo=10, hi=600, group=GROUP_ORDERS,
        label="IBKR · subscriber order status (idle)",
        why=(
            "How often we check an IBKR subscriber's orders when nothing has "
            "happened recently. Slower, to save requests."
        ),
    ),
    # ── Order fill ──────────────────────────────────────────────────────
    IntKnob(
        name="snaptrade_nudge_read_delay_s",
        setting="snaptrade_nudge_read_delay_s",
        broker="snaptrade", row="Fill nudge · first read",
        lo=1, hi=60, group=GROUP_FILL,
        label="SnapTrade fill nudge · first read",
        why=(
            "After placing a mirror order on SnapTrade, how long we wait before "
            "reading it back to see if it filled. SnapTrade shows slightly "
            "delayed data, so we ask it to refresh first and then wait this long. "
            "Too short and the refresh hasn't happened yet; too long and the "
            "regular 30s check would have caught it anyway."
        ),
    ),
    IntKnob(
        name="snaptrade_nudge_retry_delay_s",
        setting="snaptrade_nudge_retry_delay_s",
        broker="snaptrade", row="Fill nudge · retry gap",
        lo=1, hi=120, group=GROUP_FILL,
        label="SnapTrade fill nudge · retry gap",
        why=(
            "If the first read-back finds the mirror order still unfilled, how "
            "long to wait before the second read."
        ),
    ),
    IntKnob(
        name="snaptrade_nudge_max_reads",
        setting="snaptrade_nudge_max_reads",
        broker="snaptrade", row="Fill nudge · reads per placement",
        lo=1, hi=5, group=GROUP_FILL,
        kind="count", unit_label="reads",
        label="SnapTrade fill nudge · reads per placement",
        why=(
            "How many read-backs we allow per placed mirror order. This is a "
            "number of requests, not seconds. Raising it costs extra SnapTrade "
            "requests for every mirror placed, which adds up fast when one trade "
            "fans out to many subscribers."
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
            "Hard limit on how many read-back requests per minute all mirrors "
            "together may use, no matter how many orders are placed. This is the "
            "safety valve that stops a big fanout from using up the SnapTrade "
            "allowance needed to watch the trader's orders. Lower it if SnapTrade "
            "requests get tight."
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
            "How often we refresh each Alpaca account's open P&L. Alpaca allows "
            "plenty of requests, so this can be fast. It also sets how often the "
            "Positions page refreshes in the browser, so lowering it makes that "
            "page update faster too."
        ),
    ),
    IntKnob(
        name="webull_pnl_poll_interval_s",
        setting="webull_pnl_poll_interval_s",
        broker="webull", row="P&L poll",
        lo=5, hi=300, group=GROUP_PNL,
        label="Webull · P&L poll",
        why=(
            "How often we refresh each Webull account's open P&L. Webull limits "
            "how closely calls can be bunched together, so going below 5s causes "
            "clashes with the Positions page and the risk checks that already "
            "share each read."
        ),
    ),
    IntKnob(
        name="snaptrade_pnl_poll_interval_s",
        setting="snaptrade_pnl_poll_interval_s",
        broker="snaptrade", row="P&L poll",
        lo=30, hi=600, group=GROUP_PNL,
        label="SnapTrade · P&L poll",
        why=(
            "How often we refresh each SnapTrade account's open P&L. SnapTrade's "
            "request allowance (250 a minute) is shared by the whole platform, so "
            "this is the most expensive setting here: halving it doubles the cost "
            "for every user at once. Cannot go below 30s."
        ),
    ),
    IntKnob(
        name="ibkr_pnl_poll_interval_s",
        setting="ibkr_pnl_poll_interval_s",
        broker="ibkr", row="P&L poll",
        lo=5, hi=300, group=GROUP_PNL,
        label="IBKR · P&L poll",
        why=(
            "How often we refresh each IBKR account's open P&L. Every call shares "
            "the user's single IBKR session with the order poll, so going too low "
            "can slow down fill detection."
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
            "How long a Webull positions read is reused for display before we ask "
            "Webull again. Risk checks (liquidation, take-profit/stop-loss, "
            "retries) always read live and are not affected. Raise it if the "
            "Positions page hits Webull's rate limit right after an order; the "
            "trade-off is that a just-closed position may still show for up to "
            "this long."
        ),
    ),
    IntKnob(
        name="close_reconcile_interval_s",
        setting="close_reconcile_interval_s",
        broker=None, row="Close reconciler sweep",
        lo=10, hi=600, group=GROUP_POSITIONS,
        label="Close reconciler · positions sweep",
        why=(
            "How often we read every subscriber's positions to find ones the "
            "trader has already exited but the subscriber still holds. One "
            "positions read per subscriber per check. Does nothing unless the "
            "close reconciler is turned on (it is off by default)."
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
            "How often we save each account's realised P&L for past days straight "
            "from the broker, which is what the Calendar shows. Cheap, runs about "
            "hourly. If it stops, the Calendar quietly falls back to an estimate "
            "built from Kopyya's own records."
        ),
    ),
    IntKnob(
        name="day_pnl_share_s",
        setting="day_pnl_share_s",
        broker=None, row="Day P&L shared-result window",
        lo=1, hi=120, group=GROUP_CALENDAR,
        label="Day P&L · shared-result window",
        why=(
            "How long one calculated Day P&L for an account is reused across "
            "every open tab and process. It is what stops ten open tabs turning "
            "into ten broker calls, so lowering it directly multiplies the load "
            "coming from web pages. The Calendar's 'today' figure does not use "
            "this."
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
            "How often we refresh the stored balance of accounts nobody has open "
            "in the browser. One balance request per connected account per sweep, "
            "across all brokers."
        ),
    ),
    IntKnob(
        name="broker_balance_stale_s",
        setting="broker_balance_stale_s",
        broker=None, row="Balance freshness on page load",
        lo=30, hi=3600, group=GROUP_BALANCE,
        label="Balance freshness on page load",
        why=(
            "When a Brokers, Positions or Calendar page loads, any balance older "
            "than this is refreshed from the broker on the spot. So while someone "
            "has the app open, this (not the background sweep) is what really "
            "decides how often balances are fetched. Raise it to cut requests "
            "caused by page loads."
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
            "On = trust SnapTrade's webhook to tell us about the trader's fills, "
            "so the order poll slows down to once every 60s as a safety net. This "
            "is the biggest possible saving on SnapTrade requests (about 12 a "
            "minute down to 1 per trader) and costs no speed if the webhook "
            "really is arriving. Turn it on only after you have confirmed webhook "
            "deliveries."
        ),
    ),
    BoolKnob(
        name="webull_direct_poll_enabled",
        setting="webull_direct_poll_enabled",
        broker="webull", row="Order poll on/off",
        group=GROUP_ORDERS,
        label="Webull · order list poll",
        why=(
            "Webull fills are detected two ways at once: the live stream and this "
            "order-list poll. Off = the stream becomes the only way to notice a "
            "trader's fills. The stream once went silent for two months and this "
            "poll is what kept detection working, so turn it off only if you have "
            "proof the stream is healthy."
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
        "Alpaca pushes the trader's order updates to us instantly over a live "
        "connection, so there is nothing to poll.",
    ("alpaca", "Subscriber order status · idle"):
        "Alpaca subscribers are checked at one fixed interval; there is no "
        "separate slower rate for quiet accounts.",
    ("snaptrade", "Subscriber order status · idle"):
        "SnapTrade subscribers are checked at one fixed interval; there is no "
        "separate slower rate for quiet accounts.",
    ("alpaca", "Positions read cache"):
        "Only Webull reuses recent positions reads; Alpaca always reads live.",
    ("snaptrade", "Positions read cache"):
        "Only Webull reuses recent positions reads; SnapTrade always reads "
        "live.",
    ("ibkr", "Positions read cache"):
        "Only Webull reuses recent positions reads; IBKR always reads live.",
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
    return _clamp_in_range(_stored(knob.key), env_default, knob.lo, knob.hi)


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
    if value is None:
        _write_through(knob.key, None)
        return
    if value < knob.lo or value > knob.hi:
        raise ValueError(f"{knob.name} must be between {knob.lo} and {knob.hi}")
    _write_through(knob.key, str(value))


def get_bool_knob(name: str) -> bool:
    """Effective value for a switch. Same fallback contract as get_int_knob."""
    knob = _BOOL_BY_NAME[name]
    env_default = bool(getattr(get_settings(), knob.setting))
    parsed = _coerce_optional_bool(_stored(knob.key))
    return env_default if parsed is None else parsed


def set_bool_knob(name: str, value: bool | None) -> None:
    """Admin write. ``value=None`` clears the override."""
    knob = _BOOL_BY_NAME[name]
    _write_through(knob.key, None if value is None else ("1" if value else "0"))


def get_polling_knobs_state() -> dict[str, list[dict]]:
    """Everything the admin page needs for one render: per knob the default,
    the override (None when unset OR stored out-of-bounds, so the UI's '-'
    is truthful), the effective value, and the bounds for the input."""
    ints: list[dict] = []
    for knob in INT_KNOBS:
        env_default = _env_int(knob)
        override = _coerce_optional_in_range(_stored(knob.key), knob.lo, knob.hi)
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
        raw = _stored(bknob.key)
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
