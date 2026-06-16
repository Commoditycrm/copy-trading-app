from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# Explicit pool sizing. Kept deliberately modest because the app now runs as
# MULTIPLE processes (web: uvicorn --workers N, plus one `worker` process for
# listeners/poller). Total server connections = (web_workers + 1) * (pool_size
# + max_overflow), and Postgres max_connections is 100. At 10+10 per process
# that's 20/process → up to 4 processes (e.g. 3 web + 1 worker = 80) stay
# safely under 100. pool_pre_ping recycles connections the DB closed (e.g. via
# idle_in_transaction_session_timeout); pool_timeout caps the wait for a free
# connection so a request errors instead of hanging.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_timeout=10,
    pool_recycle=1800,
    future=True,
)
# expire_on_commit=False: don't expire ORM objects after commit. By default
# SQLAlchemy marks every attribute "expired" on commit, so the NEXT attribute
# access (e.g. serializing the response model) issues a fresh SELECT to reload
# it — an extra DB round-trip per request, which is costly on a remote DB.
# With this off, the in-memory state is kept (Postgres INSERT…RETURNING already
# loaded server defaults at flush), so handlers that commit-then-return the
# object serialize without a reload. Handlers that need fresh DB state after a
# commit must call db.refresh(obj) explicitly.
SessionLocal = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, future=True,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
