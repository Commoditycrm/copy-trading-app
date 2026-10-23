"""Count every request this app makes to Webull, and say what made it.

Webull rate-limits per app key, and several parts of the app call it on their
own schedules (the Positions page, the order poll, the P&L poller, auto-trim,
balance refreshes, sign-in). This answers "how many calls, and why?":

* every HTTP request the Webull SDK sends — each retry included — is counted at
  the one place they all pass through (``ApiClient._handle_single_request``);
* each is tagged with its CALLER: the page / API route that triggered it (set by
  a middleware), a background loop that tagged itself, else the thread's name;
* counts go to Redis per minute, so the web and worker processes add up;
* each call is also logged on the ``webull.usage`` logger.

``summary()`` serves GET /api/brokers/webull-usage, shown on the Positions page.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import re
import threading
import time
from typing import Any, Iterable

log = logging.getLogger("webull.usage")

_caller: contextvars.ContextVar[str | None] = contextvars.ContextVar("webull_caller", default=None)
_KEY = "webull:usage:{}"            # one Redis hash per minute
# Long enough to look back at a trading day after the close, and over a weekend
# at Friday (the trader's ask, 2026-10-23: today's 429s had already gone by the
# evening with two hours kept).
_KEEP_S = 3 * 24 * 3600
_REFUSALS_KEY = "webull:usage:429:{}"       # one list per app key: the refused calls, newest first
_REFUSALS_MAX = 500
_RECENT_KEY = "webull:usage:recent:{}:{}"   # per app key + endpoint: send times of the last few seconds
_installed = False

# Friendlier names for the app's own routes (path with ids replaced by {id}).
_ROUTE_LABELS = {
    "GET /api/positions": "Positions page",
    "GET /api/positions/day-pnl": "Positions · Day P&L",
    "GET /api/brokers": "Broker list",
    "POST /api/brokers/{id}/refresh-balance": "Balance refresh",
    "GET /api/options/quote": "Trade panel · option quote",
    "GET /api/options/chain": "Trade panel · option chain",
    "POST /api/discord-sources/internal/messages": "Discord alert (orders)",
    "GET /api/brokers/webull-usage": "Webull usage readout",
}
_UUID_RE = re.compile(r"/[0-9a-fA-F-]{32,36}(?=/|$)")


def app_hash(app_key: str | None) -> str:
    """A short, non-reversible id for an app key (the key itself is a secret)."""
    return hashlib.sha256((app_key or "").encode()).hexdigest()[:16]


@contextlib.contextmanager
def tag(caller: str):
    """Attribute Webull calls made inside this block to ``caller``."""
    token = _caller.set(caller)
    try:
        yield
    finally:
        _caller.reset(token)


def tagged(caller: str):
    """Decorator form of ``tag`` for a loop's entry point — sync or async (an
    async one is tagged inside its own task, so its to_thread calls inherit it)."""
    import functools  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _a(*args, **kwargs):
                _caller.set(caller)
                return await fn(*args, **kwargs)
            return _a

        @functools.wraps(fn)
        def _s(*args, **kwargs):
            with tag(caller):
                return fn(*args, **kwargs)
        return _s
    return deco


def set_request_caller(method: str, path: str):
    """For the HTTP middleware: tag calls made while serving this request."""
    route = f"{method.upper()} {_UUID_RE.sub('/{id}', path)}"
    return _caller.set(_ROUTE_LABELS.get(route, route))


def reset_request_caller(token) -> None:
    _caller.reset(token)


def current_caller() -> str:
    return _caller.get() or threading.current_thread().name or "unknown"


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


def record(app_key: str | None, action: str, status: Any, ms: float, caller: str | None = None,
           *, waited: float | None = None) -> None:
    caller = caller or current_caller()
    code = str(status or "error")
    log.info("webull-call %s → %s %s (%.0f ms) key=%s", caller, action, code, ms, app_hash(app_key)[:8])
    try:
        minute = int(time.time() // 60)
        r = _redis()
        field = "|".join((app_hash(app_key), caller, action, code))
        r.hincrby(_KEY.format(minute), field, 1)
        r.expire(_KEY.format(minute), _KEEP_S)
    except Exception:  # noqa: BLE001 — counting must never break a broker call
        pass
    _record_refusal(app_key, action, code, ms, caller, waited)


def _record_refusal(app_key, action: str, code: str, ms: float, caller: str, waited) -> None:
    """Keep each 429 with what explains it: when, what made it, how long it
    waited for its turn (or that its queue was full), and how many calls the
    same endpoint had sent in the 2 seconds before it.

    Many calls just before -> too many at once. Spaced calls refused anyway ->
    Webull counts stricter than the pacer spaces them. Hardly any calls from
    here just before -> something else is using the same app key."""
    import json  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    try:
        r = _redis()
        h = app_hash(app_key)
        now = time.time()
        sent_at = now - (ms or 0) / 1000.0
        recent = _RECENT_KEY.format(h, action)
        r.zadd(recent, {f"{sent_at:.3f}:{_uuid.uuid4().hex[:6]}": sent_at})
        r.zremrangebyscore(recent, 0, now - 5)
        r.expire(recent, 10)
        if code != "429":
            return
        before = max(0, int(r.zcount(recent, sent_at - 2.0, sent_at)) - 1)
        from app.services import webull_pacer  # noqa: PLC0415

        r.lpush(_REFUSALS_KEY.format(h), json.dumps({
            "at": sent_at, "caller": caller, "endpoint": action,
            "waited_s": None if waited is None or waited == webull_pacer.QUEUE_FULL else round(float(waited), 2),
            "queue_full": waited == webull_pacer.QUEUE_FULL,
            "calls_2s_before": before,
        }))
        r.ltrim(_REFUSALS_KEY.format(h), 0, _REFUSALS_MAX - 1)
        r.expire(_REFUSALS_KEY.format(h), _KEEP_S)
    except Exception:  # noqa: BLE001
        pass


def record_wait(app_key: str | None, action: str, waited_s: float) -> None:
    """A call the pacer held back: counted, with how long it waited."""
    try:
        minute = int(time.time() // 60)
        r = _redis()
        h = app_hash(app_key)
        r.hincrby(_KEY.format(minute), f"paced|{h}|{action}", 1)
        r.hincrby(_KEY.format(minute), f"paced_ms|{h}|{action}", int(waited_s * 1000))
        r.expire(_KEY.format(minute), _KEEP_S)
    except Exception:  # noqa: BLE001
        pass


def record_queue_full(app_key: str | None, action: str) -> None:
    """A call sent at once because its endpoint's queue was already longer than
    the pacer lets a caller wait: more reads wanted than Webull allows."""
    try:
        minute = int(time.time() // 60)
        r = _redis()
        r.hincrby(_KEY.format(minute), f"queue_full|{app_hash(app_key)}|{action}", 1)
        r.expire(_KEY.format(minute), _KEEP_S)
    except Exception:  # noqa: BLE001
        pass


_FRIENDLY = (
    ("assets/positions", "positions read"),
    ("assets/balance", "balance read"),
    ("orders/list-today", "today's-orders read"),
    ("order/detail", "order-status read"),
    ("order/cancel", "cancel"),
    ("order/replace", "order change"),
    ("order/place", "order placement"),
)


def friendly(action: str) -> str:
    """"/openapi/assets/positions" -> "positions read"."""
    for suffix, name in _FRIENDLY:
        if (action or "").rstrip("/").endswith(suffix):
            return name
    return action or "call"


def _note_on_position(action: str, waited: float) -> None:
    """A 429 while acting on one position: say so in that position's summary
    (services/position_events). Nothing when no position is being acted on."""
    try:
        from app.services import position_events, webull_pacer  # noqa: PLC0415

        if position_events.current_position() is None:
            return
        if waited == webull_pacer.QUEUE_FULL:
            how = "sent without waiting — the queue for it was full"
        elif waited > 0:
            how = f"after waiting {waited:.1f}s for its turn"
        else:
            how = "with no wait"
        position_events.record_issue(
            "rate_limited",
            f"Webull refused a {friendly(action)} (rate limit, 429), {how}. Made by {current_caller()}.",
            dedupe_s=60,
        )
    except Exception:  # noqa: BLE001 — a note is never worth breaking a broker call
        pass


def install() -> None:
    """Wrap the Webull SDK's single-request method. Idempotent; a missing SDK
    is fine (nothing calls Webull then)."""
    global _installed
    if _installed:
        return
    try:
        from webull.core.client import ApiClient  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    original = ApiClient._handle_single_request

    def _counted(self, endpoint, request, *args, **kwargs):
        try:
            action = request.get_action_name() if hasattr(request, "get_action_name") else "?"
        except Exception:  # noqa: BLE001
            action = "?"
        action = str(action or "?")
        app_key = getattr(self, "_app_key", None)
        # Take a turn on the endpoint's 2-per-2s counter before sending, rather
        # than be refused and retry (services/webull_pacer.py).
        waited = 0.0
        try:
            from app.services import webull_pacer  # noqa: PLC0415

            waited = webull_pacer.wait_turn(app_hash(app_key), action)
            if waited == webull_pacer.QUEUE_FULL:
                record_queue_full(app_key, action)
            elif waited > 0:
                record_wait(app_key, action, waited)
        except Exception:  # noqa: BLE001 — pacing must never break a broker call
            pass
        t0 = time.perf_counter()
        status = None
        try:
            result = original(self, endpoint, request, *args, **kwargs)
            status = result[0] if isinstance(result, tuple) and result else None
            return result
        finally:
            record(app_key, action, status, (time.perf_counter() - t0) * 1000, waited=waited)
            if str(status) == "429":
                _note_on_position(action, waited)

    ApiClient._handle_single_request = _counted
    _installed = True


def summary(app_keys: Iterable[str], minutes: int = 5) -> dict:
    """Calls in the last ``minutes`` for these app keys: total, how many were
    rate-limited (429), and the split by caller and by Webull endpoint."""
    hashes = {app_hash(k) for k in app_keys if k}
    now_min = int(time.time() // 60)
    total = limited = 0
    by_caller: dict[str, dict[str, int]] = {}
    by_endpoint: dict[str, int] = {}
    limited_by: dict[tuple[str, str], int] = {}
    paced: dict[str, dict[str, int]] = {}
    queue_full: dict[str, int] = {}
    per_minute: list[dict] = []
    try:
        r = _redis()
        mins = list(range(now_min - minutes + 1, now_min + 1))
        try:
            pipe = r.pipeline()
            for m in mins:
                pipe.hgetall(_KEY.format(m))
            raws = pipe.execute()
        except Exception:  # noqa: BLE001 — a client without pipelines
            raws = [r.hgetall(_KEY.format(m)) for m in mins]
        for m, raw in zip(mins, raws):
            raw = raw or {}
            count = 0
            for field, n in raw.items():
                field = field.decode() if isinstance(field, bytes) else field
                if field.startswith("queue_full|"):
                    _kind, h, action = field.split("|", 2)
                    if h in hashes:
                        queue_full[action] = queue_full.get(action, 0) + int(n)
                    continue
                if field.startswith(("paced|", "paced_ms|")):
                    kind, _sep, rest = field.partition("|")
                    h, _sep, action = rest.partition("|")
                    if h in hashes:
                        p = paced.setdefault(action, {"calls": 0, "waited_ms": 0})
                        p["calls" if kind == "paced" else "waited_ms"] += int(n)
                    continue
                parts = field.split("|", 3)
                if len(parts) != 4 or parts[0] not in hashes:
                    continue
                _h, caller, action, code = parts
                n = int(n)
                count += n
                total += n
                c = by_caller.setdefault(caller, {"calls": 0, "rate_limited": 0})
                c["calls"] += n
                if code == "429":
                    c["rate_limited"] += n
                    limited += n
                    limited_by[(caller, action)] = limited_by.get((caller, action), 0) + n
                by_endpoint[action] = by_endpoint.get(action, 0) + n
            per_minute.append({"minute": m * 60, "calls": count})
    except Exception:  # noqa: BLE001
        log.warning("webull usage summary failed", exc_info=True)
    return {
        "minutes": minutes,
        "total": total,
        "rate_limited": limited,
        "per_minute": per_minute,
        "by_caller": sorted(
            ({"caller": k, **v} for k, v in by_caller.items()), key=lambda x: -x["calls"],
        ),
        "by_endpoint": sorted(
            ({"endpoint": k, "calls": v} for k, v in by_endpoint.items()), key=lambda x: -x["calls"],
        ),
        # Each 429: what made the call, and which Webull endpoint refused it.
        "rate_limited_by": sorted(
            ({"caller": c, "endpoint": e, "calls": v} for (c, e), v in limited_by.items()),
            key=lambda x: -x["calls"],
        ),
        # Calls the pacer held back for their turn instead of letting them be
        # refused (services/webull_pacer.py), and how long they waited.
        "paced": sorted(
            ({"endpoint": k, "calls": v["calls"],
              "avg_wait_ms": round(v["waited_ms"] / v["calls"]) if v["calls"] else 0}
             for k, v in paced.items()),
            key=lambda x: -x["calls"],
        ),
        # Calls sent without waiting because their endpoint's queue was already
        # full — more reads wanted than Webull allows. These are the ones that
        # can still be refused.
        "queue_full": sorted(
            ({"endpoint": k, "calls": v} for k, v in queue_full.items()), key=lambda x: -x["calls"],
        ),
        # Each refused call in the window, newest first, with what explains it
        # (see _record_refusal).
        "refusals": _refusals(hashes, now_min * 60 - (minutes - 1) * 60),
    }


def _refusals(hashes: set[str], since: float, limit: int = 100) -> list[dict]:
    import json  # noqa: PLC0415

    out: list[dict] = []
    try:
        r = _redis()
        for h in hashes:
            for raw in r.lrange(_REFUSALS_KEY.format(h), 0, _REFUSALS_MAX - 1) or []:
                try:
                    e = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if float(e.get("at") or 0) >= since:
                    out.append(e)
    except Exception:  # noqa: BLE001
        return out
    out.sort(key=lambda e: -float(e.get("at") or 0))
    return out[:limit]


__all__ = ["tag", "tagged", "install", "record", "record_wait", "record_queue_full", "summary", "app_hash",
           "set_request_caller", "reset_request_caller", "current_caller"]
