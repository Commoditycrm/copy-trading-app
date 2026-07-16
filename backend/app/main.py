import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin as admin_api,
    auth,
    brokers,
    events,
    follow_requests,
    listener as listener_api,
    notifications as notifications_api,
    options,
    performance,
    positions,
    settings,
    subscribers,
    trades,
)
from app.config import get_settings
from app.services import events as events_bus
from app.services import listeners, pnl_poller, recovery, retry_scheduler
from app.services.redis_client import close_async_redis

import os as _os  # noqa: E402

# Make application logs visible. With no logging config at all, app loggers
# (app.*) sit below the root's default WARNING and emit NOTHING — which made
# the worker effectively undebuggable (listener/reconcile/error lines never
# surfaced). Configure a root handler so INFO+ reaches stderr → container logs.
# Honors LOG_LEVEL if set. Done at import time, before any app logger fires.
logging.basicConfig(
    level=_os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

# On-demand thread-stack dump for diagnosing a hung worker (event loop blocked
# on a lock). `docker compose exec worker kill -USR1 1` prints every thread's
# Python traceback to stderr (→ container logs) WITHOUT killing the process.
# faulthandler.enable() also dumps on a hard crash. No-op where SIGUSR1 is
# unavailable (Windows dev).
import faulthandler as _faulthandler  # noqa: E402
import signal as _signal  # noqa: E402

_faulthandler.enable()
try:
    _faulthandler.register(_signal.SIGUSR1, all_threads=True)
except (AttributeError, ValueError):  # pragma: no cover — SIGUSR1 not on Windows
    pass

# Strong references to fire-and-forget background tasks. asyncio.create_task
# only keeps a WEAK reference, so a task whose return value isn't stored can be
# garbage-collected mid-flight and silently cancelled (per the asyncio docs).
# The listeners survive because trade_listener._tasks holds them; these don't
# have a natural owner, so we park them here for the life of the process.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """create_task + keep a strong ref until the task finishes, so the GC can't
    reap a long-lived background loop out from under us."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


DISCLAIMER = (
    "Educational software. Not investment advice. Copy trading involves substantial risk "
    "of loss. The platform operator may need to register as an investment adviser under "
    "applicable securities laws (e.g. US SEC/FINRA) before charging subscribers. "
    "Verify your regulatory obligations before going live."
)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Copy Trading Platform",
        version="0.2.0",
        description=DISCLAIMER,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(admin_api.router)
    app.include_router(auth.router)
    app.include_router(brokers.router)
    app.include_router(trades.router)
    app.include_router(settings.router)
    app.include_router(subscribers.router)
    app.include_router(follow_requests.router)
    app.include_router(events.router)
    app.include_router(options.router)
    app.include_router(positions.router)
    app.include_router(performance.router)
    app.include_router(listener_api.router)
    app.include_router(notifications_api.router)

    # Shared across the startup/shutdown hooks so the retry scheduler
    # thread can be signalled to exit cleanly when uvicorn shuts down.
    shutdown_event = threading.Event()
    scheduler_thread: threading.Thread | None = None

    @app.on_event("startup")
    async def _bind_loop() -> None:
        loop = asyncio.get_running_loop()
        events_bus.bind_loop(loop)
        # Capture the main loop reference so sync request handlers (POST
        # /api/brokers) can schedule listener tasks on the right loop
        # without needing to be async themselves. Without this, adding a
        # broker at runtime silently fails to start its listener and the
        # user has to restart the backend container. Both the Alpaca
        # WebSocket listener and the Webull poll listener share this
        # captured loop.
        listeners.bind_loop(loop)
        pnl_poller.bind_loop(loop)
        # Replace the default ThreadPoolExecutor (capped at min(32, cpu+4)) so
        # asyncio.to_thread() can actually run 200 broker calls in parallel
        # during fanout. Without this, the semaphore is misleading — calls
        # would queue at the threadpool instead of going out concurrently.
        loop.set_default_executor(
            ThreadPoolExecutor(
                max_workers=s.fanout_threadpool_size,
                thread_name_prefix="fanout",
            )
        )

        # ── Web/worker split ──────────────────────────────────────────────
        # Background singletons (broker listeners, P&L poller, retry
        # scheduler, crash-recovery sweep) must run in EXACTLY ONE process.
        # Under uvicorn --workers N every web worker runs this startup hook,
        # so the web container sets run_background_workers=false and returns
        # here; only the dedicated `worker` container (flag true) starts them.
        # Otherwise each worker would spawn its own listeners/poller →
        # duplicated broker API calls and double-processed fills. The
        # threadpool above stays in web workers because fanout (request-path)
        # still runs there.
        if not s.run_background_workers:
            log.info(
                "web mode (run_background_workers=false): skipping "
                "listeners / pnl_poller / retry-scheduler in this process"
            )
            return

        # Replay any PENDING child orders stranded by a previous crash before
        # we start serving traffic. Failures here are logged, never fatal.
        try:
            recovered = await recovery.sweep_orphaned_pending()
            if recovered:
                log.info("recovery sweep replayed %d orphaned PENDING orders", recovered)
        except Exception:  # noqa: BLE001
            log.exception("recovery sweep failed")
        # Spawn listeners for every active trader's connected broker —
        # Alpaca WebSocket and/or Webull poll loop. Requires a long-running
        # process; won't work on Vercel serverless.
        try:
            await listeners.start_all_listeners()
        except Exception:  # noqa: BLE001
            log.exception("failed to start trade listeners")

        # Listen for start/stop requests from the web tier (a trader
        # connecting/reconnecting a broker). The web container can't start
        # listeners itself without duplicating ours, so it hands them over
        # this channel. Worker-only — runs for the life of the process.
        try:
            from app.services import listener_control
            _spawn_background(listener_control.run_subscriber())
        except Exception:  # noqa: BLE001
            log.exception("failed to start listener_control subscriber")

        # Safety net for the pub/sub handoff above: every 15s reconcile the
        # listeners running in this process against the connected trader
        # brokers in the DB, starting any that are missing. Without this a
        # control message missed during a restart/Redis blip leaves a
        # connected trader with no listener (status pill stuck offline).
        try:
            _spawn_background(listeners.run_reconciler())
        except Exception:  # noqa: BLE001
            log.exception("failed to start listener reconciler")

        # 60s sweep that pulls today's P&L from Alpaca directly and
        # auto-pauses copy when loss/profit limits are hit. Covers the
        # "trader is quiet but subscriber's positions move" gap that
        # copy_engine's in-fanout check can't see.
        try:
            pnl_poller.start()
        except Exception:  # noqa: BLE001
            log.exception("failed to start pnl_poller")

        # End-of-day safety net: at 15:55 ET, market-close every subscriber's
        # SAME-DAY-EXPIRY option positions so a trader who forgets to flatten
        # 0DTE contracts doesn't strand subscribers holding them into expiry.
        # Loop checks the clock every 30s and fires once per trading day; the
        # matching last-5-minutes new-order lockout lives in copy_engine fanout.
        try:
            from app.services import eod_autoclose
            _spawn_background(eod_autoclose.run_loop(shutdown_check=shutdown_event.is_set))
        except Exception:  # noqa: BLE001
            log.exception("failed to start eod_autoclose loop")

        # Alpaca subscriber mirror-fill reconciler — the Alpaca twin of the
        # SnapTrade subscriber reconciler. Alpaca subscriber accounts have no
        # real-time listener, so without this a mirror that fills there can stay
        # WORKING in our DB (breaking close-detection + order history). Every 30s
        # it refreshes only those orders' status from Alpaca. Worker-only.
        try:
            from app.services import alpaca_subscriber_reconciler
            alpaca_subscriber_reconciler.start_alpaca_subscriber_reconciler()
        except Exception:  # noqa: BLE001
            log.exception("failed to start alpaca subscriber reconciler")

        # Start the retry scheduler in a daemon thread. It polls every 10s
        # for RETRY_PENDING orders whose retry_at has elapsed and runs the
        # broker call again. Daemon=True so the thread doesn't keep
        # uvicorn alive on a hard stop, and the shutdown_event lets the
        # graceful path tell it to exit at the top of the next iteration.
        nonlocal scheduler_thread
        scheduler_thread = threading.Thread(
            target=retry_scheduler.poll_loop,
            kwargs={"shutdown_check": shutdown_event.is_set},
            name="retry-scheduler",
            daemon=True,
        )
        scheduler_thread.start()

    @app.on_event("shutdown")
    async def _stop_listeners() -> None:
        # Signal the retry scheduler to exit at its next poll tick. We
        # don't join — daemon=True takes care of hard termination if it
        # doesn't notice in time, and joining would block shutdown on
        # the (up to 10s) sleep at the bottom of the loop.
        shutdown_event.set()
        try:
            await listeners.stop_all_listeners()
        except Exception:  # noqa: BLE001
            log.exception("failed to stop trade listeners cleanly")
        try:
            await pnl_poller.stop()
        except Exception:  # noqa: BLE001
            log.exception("failed to stop pnl_poller cleanly")
        try:
            from app.services import alpaca_subscriber_reconciler
            await alpaca_subscriber_reconciler.stop_alpaca_subscriber_reconciler()
        except Exception:  # noqa: BLE001
            log.exception("failed to stop alpaca subscriber reconciler cleanly")
        try:
            await close_async_redis()
        except Exception:  # noqa: BLE001
            log.exception("failed to close redis client cleanly")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "disclaimer": DISCLAIMER}

    return app


app = create_app()
