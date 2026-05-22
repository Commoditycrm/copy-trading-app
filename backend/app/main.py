import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, brokers, events, listener as listener_api, options, performance, positions, settings, subscribers, trades
from app.config import get_settings
from app.services import events as events_bus
from app.services import recovery, trade_listener
from app.services.redis_client import close_async_redis

log = logging.getLogger(__name__)

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

    app.include_router(auth.router)
    app.include_router(brokers.router)
    app.include_router(trades.router)
    app.include_router(settings.router)
    app.include_router(subscribers.router)
    app.include_router(events.router)
    app.include_router(options.router)
    app.include_router(positions.router)
    app.include_router(performance.router)
    app.include_router(listener_api.router)

    @app.on_event("startup")
    async def _bind_loop() -> None:
        loop = asyncio.get_running_loop()
        events_bus.bind_loop(loop)
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
        # Replay any PENDING child orders stranded by a previous crash before
        # we start serving traffic. Failures here are logged, never fatal.
        try:
            recovered = await recovery.sweep_orphaned_pending()
            if recovered:
                log.info("recovery sweep replayed %d orphaned PENDING orders", recovered)
        except Exception:  # noqa: BLE001
            log.exception("recovery sweep failed")
        # Spawn Alpaca trade_updates listeners for every active trader with a
        # connected Alpaca account. Requires a long-running process — won't
        # work on Vercel serverless.
        try:
            await trade_listener.start_all_listeners()
        except Exception:  # noqa: BLE001
            log.exception("failed to start trade listeners")

    @app.on_event("shutdown")
    async def _stop_listeners() -> None:
        try:
            await trade_listener.stop_all_listeners()
        except Exception:  # noqa: BLE001
            log.exception("failed to stop trade listeners cleanly")
        try:
            await close_async_redis()
        except Exception:  # noqa: BLE001
            log.exception("failed to close redis client cleanly")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "disclaimer": DISCLAIMER}

    return app


app = create_app()
