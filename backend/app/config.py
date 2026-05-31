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
    # SnapTrade credentials — empty by default so dev environments work
    # without setting them up. The /api/brokers/snaptrade/* endpoints
    # return 503 when these are blank rather than crashing. Get them from
    # https://dashboard.snaptrade.com/.
    snaptrade_client_id: str = ""
    snaptrade_consumer_key: str = ""
    # Set true once you've configured a SnapTrade webhook listener +
    # Trade Detection. The webhook then drives near-real-time detection,
    # so our own SnapTrade poll-listener backs off to a 60s backstop
    # interval instead of polling every 5s (saves API calls / rate-limit
    # headroom). False = no webhook, poller stays at its 5s cadence.
    snaptrade_webhook_enabled: bool = False
    # asyncio.to_thread() uses the default ThreadPoolExecutor (default size
    # min(32, cpu+4) — way too small for 200 concurrent broker calls). We
    # bump this at startup so all 200 actually run in parallel.
    fanout_threadpool_size: int = 256
    # Cache TTLs (seconds) — short by design; invalidated on writes too.
    cache_ttl_subscribers: int = 60
    cache_ttl_broker_accounts: int = 300
    # Fanout-batch threshold. Below this subscriber count, copy_engine
    # runs the per-iteration code path (one db.get(User) + one
    # cache.get_broker_accounts per sub) — lower first-sub pick_lag floor
    # (~30ms) at the cost of linear-in-N total. At/above this count it
    # switches to the batched code path (three pre-SELECTs up front) —
    # higher floor (~150-300ms) but flat scaling, so 1000+ subs finish in
    # the same wall-clock as 100. Admin can override at runtime via Redis
    # (see services.platform_config); env var sets the default.
    fanout_batch_threshold: int = 75

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
