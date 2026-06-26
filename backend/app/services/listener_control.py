"""Cross-process listener control channel (web tier -> worker).

The web/worker split says background singletons — broker listeners, P&L
poller, retry scheduler — run in EXACTLY ONE process: the dedicated
``worker`` container (``run_background_workers=true``). The web container
runs uvicorn with the flag false so it never starts them.

But listeners must be (re)started in response to web-tier actions: a trader
connecting or reconnecting a broker hits the web container. Historically the
web handler called ``start_listener`` directly, which spun up a listener IN
THE WEB PROCESS — on top of the worker's listener. Two listeners on the same
broker stream each insert a parent order per trade, so the subscriber gets
TWO mirror copies (the "doubling" bug).

Fix: the web tier PUBLISHES a start/stop request here; the worker SUBSCRIBES
and performs the action in its own (single) process. Single-process
deployments (flag true everywhere) skip the channel and act directly — see
``listeners.start_listener``.

Best-effort, like the rest of our Redis use: if Redis is down the request is
lost, but the worker re-derives listeners from the DB on its next restart via
``start_all_listeners``, so the worst case is a delayed listener, never a
duplicate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from app.services.redis_client import get_async_redis, get_sync_redis

log = logging.getLogger(__name__)

_CHANNEL = "listener:control"


def request_start(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    _publish({
        "action": "start",
        "trader": str(trader_user_id),
        "account": str(broker_account_id),
    })


def request_stop(trader_user_id: uuid.UUID) -> None:
    _publish({"action": "stop", "trader": str(trader_user_id)})


def _publish(msg: dict) -> None:
    try:
        get_sync_redis().publish(_CHANNEL, json.dumps(msg))
        log.info("listener_control: published %s", msg)
    except Exception:  # noqa: BLE001
        log.exception("listener_control: publish failed for %s", msg)


async def run_subscriber() -> None:
    """Worker-side loop: consume control messages and apply them in THIS
    process. Runs for the life of the worker; reconnects on Redis error."""
    # Lazy import — listeners imports lots of service modules; importing it at
    # module load would risk a cycle through main's startup wiring.
    from app.services import listeners

    backoff = 1.0
    while True:
        try:
            pubsub = get_async_redis().pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(_CHANNEL)
            log.info("listener_control: subscribed to %s", _CHANNEL)
            backoff = 1.0
            try:
                while True:
                    msg = await pubsub.get_message(timeout=5.0)
                    if msg is None:
                        continue
                    data = msg.get("data")
                    if data is None:
                        continue
                    await _dispatch(listeners, data)
            finally:
                try:
                    await pubsub.unsubscribe(_CHANNEL)
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception(
                "listener_control: subscriber error; reconnecting in %.0fs", backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)


async def _dispatch(listeners, raw) -> None:
    # CRITICAL: run the local start/stop OFF the event loop via to_thread.
    # ``trade_listener.stop_listener`` calls alpaca ``stream.stop()``, which
    # blocks on ``run_coroutine_threadsafe(close, loop).result()`` — it waits
    # for THIS loop to run the stream's shutdown coroutine. Called inline on the
    # loop, it deadlocks: the loop is stuck inside ``.result()`` so it can never
    # run the coroutine it's waiting on, and the whole worker (every listener,
    # the reconciler, the pnl poller) freezes. Offloading to a worker thread
    # keeps the loop free to run that coroutine, so stop() completes.
    # ``_start_listener_local`` is also safe off-loop: ``start_listener``
    # marshals task creation back onto the loop via ``call_soon_threadsafe``.
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        action = msg.get("action")
        if action == "start":
            await asyncio.to_thread(
                listeners._start_listener_local,  # noqa: SLF001 — same package
                uuid.UUID(msg["trader"]), uuid.UUID(msg["account"]),
            )
        elif action == "stop":
            await asyncio.to_thread(
                listeners._stop_listener_local,  # noqa: SLF001
                uuid.UUID(msg["trader"]),
            )
        else:
            log.warning("listener_control: unknown action in %s", msg)
    except Exception:  # noqa: BLE001
        log.exception("listener_control: failed to dispatch %r", raw)


__all__ = ["request_start", "request_stop", "run_subscriber"]
