from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# Explicit pool sizing. The default (size 5 + overflow 10 = 15) is too small
# once the pnl_poller fans out over every connected account concurrently each
# tick; a single poll wave could consume the whole pool and starve API
# requests. pool_pre_ping recycles connections the DB closed (e.g. via
# idle_in_transaction_session_timeout). pool_timeout caps how long a request
# waits for a free connection before erroring instead of hanging.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
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
