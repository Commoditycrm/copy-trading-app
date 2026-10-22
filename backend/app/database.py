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
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def release_connection(db: Session) -> None:
    """End db's transaction so its connection goes back to the pool, keeping
    loaded objects readable. Call before slow broker I/O: a request session
    otherwise sits "idle in transaction" on a pooled connection for the whole
    call (QA 2026-10-21: 40 concurrent page loads exhausted 10+10 this way).
    The next query checks out a fresh connection. Commits, so only use where
    nothing pending needs to stay atomic with later writes."""
    if not db.in_transaction():
        return
    keep = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = keep
