from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 30
    jwt_refresh_token_days: int = 14
    credential_encryption_key: str
    cors_origins: str = "http://localhost:3000"
    frontend_base_url: str = "http://localhost:3000"
    redis_url: str = "redis://localhost:6379/0"
    # Per-broker concurrent-request cap during fanout. Tune down if you hit 429s.
    broker_concurrency_alpaca: int = 200
    # asyncio.to_thread() uses the default ThreadPoolExecutor (default size
    # min(32, cpu+4) — way too small for 200 concurrent broker calls). We
    # bump this at startup so all 200 actually run in parallel.
    fanout_threadpool_size: int = 256
    # Cache TTLs (seconds) — short by design; invalidated on writes too.
    cache_ttl_subscribers: int = 60
    cache_ttl_broker_accounts: int = 300

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
