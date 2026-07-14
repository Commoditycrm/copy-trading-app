from functools import lru_cache
from pydantic import AliasChoices, Field
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
    # Web/worker split. Background singletons (broker listeners, P&L poller,
    # retry scheduler, crash-recovery sweep) must run in EXACTLY ONE process.
    # The dedicated `worker` container sets this true; the web container runs
    # uvicorn --workers N with it FALSE so those services aren't duplicated
    # per worker (which would double broker API calls + double-process fills).
    # Defaults true so a single-process deployment keeps working unchanged.
    run_background_workers: bool = True
    # How often (seconds) the WORKER reconciles its running broker listeners
    # against the DB. This is what makes a broker connected/disconnected at
    # runtime in the WEB container get its listener started/stopped without a
    # worker restart (the web/worker split can't start a task cross-process).
    # Only used when run_background_workers=true.
    listener_reconcile_interval_s: float = 15.0
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
    # Alpaca pnl_poller per-account interval (seconds). Pnl_poller hits
    # one Alpaca GET /v2/account per subscriber per tick — at the
    # default 10s that's 6 req/min/account against Alpaca's 200/min
    # budget. Admin can override at runtime via Redis (see
    # services.platform_config); env var sets the default. Bounds
    # enforced in the setter: 5-300s.
    alpaca_pnl_poll_interval_s: int = 10
    # ── Password reset / transactional email (SendGrid) ───────────────────
    # SendGrid Web API v3 key. Blank by default so dev/QA work without it —
    # the email service then logs the reset link instead of sending (see
    # services/email.py). Get a key at https://app.sendgrid.com/settings/api_keys.
    sendgrid_api_key: str = ""
    # The From address + display name on outgoing mail. The from address MUST
    # be a verified sender / authenticated domain in SendGrid or sends are
    # rejected (403). Accepts either EMAIL_FROM or SENDGRID_FROM_EMAIL as the
    # env var name (the SendGrid-style name is what the dashboard guides toward).
    email_from: str = Field(
        default="noreply@kopyya.com",
        validation_alias=AliasChoices("email_from", "sendgrid_from_email"),
    )
    email_from_name: str = "Kopyya"
    # If set, password-reset emails are sent via this SendGrid Dynamic Template
    # (designed in the SendGrid UI) instead of the built-in inline HTML. The
    # template is passed this dynamic data (handlebars): {{reset_link}},
    # {{name}}, {{app_name}}, {{expiry_minutes}}. Leave blank to use inline HTML.
    sendgrid_password_reset_template_id: str = ""
    # Password-reset link lifetime. Short by design — long enough to act on the
    # email, short enough to limit exposure if the inbox is later compromised.
    password_reset_token_minutes: int = 30
    # Email-verification link lifetime. Longer than a reset (24h) — verification
    # emails often sit in an inbox a while before the user clicks.
    email_verification_token_minutes: int = 1440
    # Optional SendGrid Dynamic Template for the verification email. Receives
    # {{verify_link}}, {{name}}, {{app_name}}. Blank → built-in inline HTML.
    sendgrid_verification_template_id: str = ""

    # ── SMS (Twilio) ──────────────────────────────────────────────────────
    # Twilio REST credentials — Account SID + Auth Token from the Twilio Console
    # dashboard. Blank by default so dev/QA work without them: services/sms.py
    # then logs the message instead of sending, keeping SMS flows testable.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # Preferred sender: a Messaging Service SID (MG…) from Twilio Console →
    # Messaging → Services. Owns the sender pool, opt-out handling and retries.
    # If blank we fall back to twilio_from_number (a single SMS-capable Twilio
    # number in E.164, e.g. +15551234567). One of the two must be set to send.
    twilio_messaging_service_sid: str = ""
    twilio_from_number: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
