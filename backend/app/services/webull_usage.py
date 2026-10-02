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
_KEEP_S = 2 * 3600
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


def record(app_key: str | None, action: str, status: Any, ms: float, caller: str | None = None) -> None:
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
        t0 = time.perf_counter()
        status = None
        try:
            result = original(self, endpoint, request, *args, **kwargs)
            status = result[0] if isinstance(result, tuple) and result else None
            return result
        finally:
            try:
                action = request.get_action_name() if hasattr(request, "get_action_name") else "?"
            except Exception:  # noqa: BLE001
                action = "?"
            record(getattr(self, "_app_key", None), str(action or "?"), status,
                   (time.perf_counter() - t0) * 1000)

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
    per_minute: list[dict] = []
    try:
        r = _redis()
        for m in range(now_min - minutes + 1, now_min + 1):
            raw = r.hgetall(_KEY.format(m)) or {}
            count = 0
            for field, n in raw.items():
                field = field.decode() if isinstance(field, bytes) else field
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
    }


__all__ = ["tag", "tagged", "install", "record", "summary", "app_hash",
           "set_request_caller", "reset_request_caller", "current_caller"]
