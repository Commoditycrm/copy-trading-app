"""Per-user event bus for SSE, backed by Redis pub/sub.

Why Redis: an SSE connection is held by exactly one FastAPI worker, but events
can be published from any worker (or from a background task running on a
different process). Redis pub/sub gives us cross-process fan-out for free.

Channel convention: `events:user:{user_id}` — one channel per recipient. We
don't multiplex; the keyspace is tiny (one channel per active SSE connection)
and per-user filtering is just a SUBSCRIBE.

Failure mode: if Redis is unreachable, publish is a no-op (event is lost) and
subscribe yields heartbeats only. The canonical state is always in Postgres,
so the SSE feed is lossy by design — a missed event just means the UI is
slightly stale until the user navigates / refetches.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)


def _channel(user_id: uuid.UUID) -> str:
    return f"events:user:{user_id}"


# Global channel every admin SSE connection also subscribes to. Order-lifecycle
# events are mirrored here so the admin panel updates live — per-user channels
# only reach the order's OWNER, never an admin watching the whole platform.
_ADMIN_CHANNEL = "events:admin"

# Global channel carrying live market-data ticks (price.tick). EVERY SSE
# connection subscribes to it so a held symbol's price updates on screen without
# a refresh; the frontend ignores ticks for symbols it isn't showing. Fed by the
# market-data streams (throttled, ~1 tick/sec/symbol), so it's low-volume.
_PRICES_CHANNEL = "events:prices"


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """No-op now — kept for backward compatibility with main.py's startup
    hook. Redis pub/sub doesn't need a bound loop because publish is sync
    (executes synchronously against the sync redis client) and subscribe runs
    on whatever loop awaits it."""
    return None


async def subscribe(
    user_id: uuid.UUID, include_admin: bool = False, include_prices: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Subscribe to events for `user_id`. Yields decoded JSON payloads. If
    Redis is unreachable, the generator yields nothing and exits — the SSE
    endpoint's heartbeat keeps the connection alive.

    include_admin: also subscribe to the global admin channel (set for admins)
    so the admin panel receives platform-wide order-lifecycle events.
    include_prices: also subscribe to the global live-price channel so held
    symbols tick on screen (default on)."""
    channels = [_channel(user_id)]
    if include_admin:
        channels.append(_ADMIN_CHANNEL)
    if include_prices:
        channels.append(_PRICES_CHANNEL)
    r = get_async_redis()
    try:
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(*channels)
    except Exception:  # noqa: BLE001
        log.exception("redis pubsub subscribe failed for user=%s", user_id)
        return

    # Per-client price filtering: the prices channel is global, but this
    # connection should only forward ticks for symbols THIS user is actually
    # showing (held ∪ watched) — not the whole platform's firehose. Recomputed
    # periodically; a lookup failure leaves it None → fail open (forward all).
    import time as _time  # noqa: PLC0415

    async def _load_interest() -> "set[str] | None":
        try:
            from app.services import market_data_stream as _mds  # noqa: PLC0415
            return await asyncio.to_thread(_mds.user_interest, user_id)
        except Exception:  # noqa: BLE001
            return None

    interest: "set[str] | None" = await _load_interest() if include_prices else None
    interest_at = _time.monotonic()

    try:
        while True:
            # get_message returns None on timeout — we use that to let the
            # caller poll request.is_disconnected() between events.
            msg = await pubsub.get_message(timeout=1.0)
            if include_prices and _time.monotonic() - interest_at >= 10.0:
                interest = await _load_interest()
                interest_at = _time.monotonic()
            if msg is None:
                continue
            data = msg.get("data")
            if data is None:
                continue
            try:
                payload = json.loads(data) if isinstance(data, (str, bytes)) else data
            except json.JSONDecodeError:
                log.warning("dropping malformed event on channel %s", _channel(user_id))
                continue
            # Drop price ticks for symbols this client isn't showing.
            if (
                interest is not None
                and isinstance(payload, dict)
                and payload.get("type") == "price.tick"
                and str(payload.get("symbol", "")).upper() not in interest
            ):
                continue
            yield payload
    finally:
        try:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


def publish(user_id: uuid.UUID, event: dict[str, Any]) -> None:
    """Sync, fire-and-forget. Safe to call from any thread or background
    task. Drops the event silently on Redis errors.

    Order-lifecycle events (type "order.*") are also mirrored to the global
    admin channel so the admin panel updates in real time — without this, an
    admin only ever sees their OWN events, never the platform's order flow."""
    try:
        payload = json.dumps(event, default=str)
        r = get_sync_redis()
        r.publish(_channel(user_id), payload)
        if str(event.get("type", "")).startswith("order."):
            r.publish(_ADMIN_CHANNEL, payload)
    except Exception:  # noqa: BLE001
        log.warning("event publish dropped for user=%s", user_id)


def publish_price(symbol: str, price: str, bid: str | None = None, ask: str | None = None) -> None:
    """Broadcast one live price tick to the global prices channel (all SSE
    connections). Sync, fire-and-forget. Callers throttle upstream, so this just
    ships it. ``price`` is the mid; ``bid``/``ask`` ride along (when present) so
    the trade panel's quote panel ticks live. The frontend applies it to any
    on-screen row/ticket for ``symbol``."""
    try:
        evt: dict[str, Any] = {"type": "price.tick", "symbol": symbol.upper(), "price": price}
        if bid is not None:
            evt["bid"] = bid
        if ask is not None:
            evt["ask"] = ask
        get_sync_redis().publish(_PRICES_CHANNEL, json.dumps(evt))
    except Exception:  # noqa: BLE001
        pass
