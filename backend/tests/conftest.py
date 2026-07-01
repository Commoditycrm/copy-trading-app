"""Shared pytest fixtures for the QA auth suite.

Isolation: env is pinned to a dedicated test database (trading_app_test) and a
separate Redis logical DB (index 1) BEFORE any app import, so the app engine and
redis client bind to the test tiers and never touch dev data. Background workers
are forced off so importing the app spawns no broker listeners/pollers.
"""
import os

# --- must run before importing anything under app.* -------------------------
os.environ["DATABASE_URL"] = (
    "postgresql+psycopg://trading:trading@localhost:5433/trading_app_test"
)
os.environ["REDIS_URL"] = "redis://localhost:6380/1"
os.environ["RUN_BACKGROUND_WORKERS"] = "false"
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("FRONTEND_BASE_URL", "http://localhost:3000")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.database import SessionLocal, engine
from app.services.redis_client import get_sync_redis

# Tables cleared between tests. Order doesn't matter — CASCADE handles FKs.
_TABLES = [
    "audit_logs",
    "fills",
    "orders",
    "notifications",
    "daily_equity_snapshots",
    "subscriber_settings",
    "trader_settings",
    "broker_accounts",
    "test_results",
    "load_test_runs",
    "users",
]


@pytest.fixture(scope="session")
def client():
    # No context manager → no lifespan/startup, so background singletons never
    # fire even if the flag were on. Auth routes only need DB + Redis.
    return TestClient(app)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean():
    """Wipe DB tables + the test Redis index before every test for isolation."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    try:
        get_sync_redis().flushdb()
    except Exception:
        pass
    yield
