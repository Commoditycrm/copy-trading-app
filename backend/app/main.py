import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, brokers, events, settings, subscribers, trades
from app.config import get_settings
from app.services import events as events_bus

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

    @app.on_event("startup")
    async def _bind_loop() -> None:
        events_bus.bind_loop(asyncio.get_running_loop())

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "disclaimer": DISCLAIMER}

    return app


app = create_app()
