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
    # Close reconciler — flattens positions a subscriber still holds but the
    # trader has EXITED (a copied close that got canceled/blocked and never
    # re-placed). Ships DRY-RUN first, like position_reconciler:
    #   close_reconcile_enabled  → master switch for the whole loop (default off)
    #   close_reconcile_apply    → False = only LOG what it WOULD close;
    #                              True  = actually place the flatten orders.
    # Validate the dry-run log on prod before flipping apply on.
    close_reconcile_enabled: bool = False
    close_reconcile_apply: bool = False
    close_reconcile_interval_s: float = 30.0
    # ── Direct Webull integration (real-time gRPC trade signal) ──────────
    # Master switch for the direct-Webull path (adapter + trade-event
    # listener). Default OFF — when false, WEBULL broker accounts are
    # inert (no adapter routing, no listener) exactly as today, so nothing
    # in the existing SnapTrade/Alpaca/IBKR paths changes. Turn on only in
    # environments where a trader has connected a direct Webull account.
    webull_direct_enabled: bool = False
    # Shadow mode: when true, the Webull listener DETECTS + logs the
    # trader's orders but does NOT fan out to subscribers. Lets us verify
    # parity against the SnapTrade feed before trusting it with real
    # mirrors. Set false to enable live fanout from the Webull signal.
    webull_direct_shadow_mode: bool = True
    # Poll backstop for the direct-Webull signal. Webull's gRPC trade-event
    # stream requires a per-app_key push scope that isn't always enabled
    # (SubscribeSuccess + Pings arrive but no order frames). This poller pulls
    # the trader's orders from Webull's REST order API on a short interval and
    # feeds the SAME persist+fanout path (dedup by broker_order_id, so it never
    # double-fires with the stream). It's the reliable detection path; the gRPC
    # stream stays as the ~0.2s fast path for when the scope is enabled. Runs
    # only when webull_direct_enabled is on; respects shadow mode.
    webull_direct_poll_enabled: bool = True
    # Base seconds between REST order polls. Webull's "Query Day Orders"
    # endpoint caps at 10 requests / 30s PER APP ID (≈1 call / 3s), shared
    # across all accounts under one app_key. We make one call per account per
    # cycle, so the listener floors this to 3.5s and auto-scales it up by
    # account count (≈3.3s × accounts) — see webull_listener._safe_poll_interval.
    # 5s is safe for a single-account trader (6 calls / 30s) with headroom;
    # 4s also works (7.5 / 30s). Don't go below 3.5s.
    webull_poll_interval_seconds: float = 5.0
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
    # Extended-hours (pre/post-market) mirror pricing. Alpaca is LIMIT-only then,
    # so a forced entry/close is routed as a marketable limit. When we know the
    # TRADER's fill price we anchor that limit to it ± this percent (a BUY bids
    # trader_fill × (1 + pct/100); a SELL offers trader_fill × (1 − pct/100)) —
    # our own last-trade quote can diverge wildly from the trader's venue on thin
    # pre-market names, leaving the limit below the market and unfilled (prod STFS
    # 2026-07-29: our quote ~3.09 vs the trader's 4.95 fill). The percent also caps
    # how far we chase. REGULAR-hours orders are unaffected — they still go MARKET.
    mirror_ext_hours_slippage_pct: float = 3.0
    # ── End-of-day subscriber safety auto-close ───────────────────────────
    # At 15:55 ET (5 minutes before the 16:00 US close) the worker market-closes
    # every subscriber's SAME-DAY-EXPIRY (0DTE) option positions, and the fanout
    # refuses new same-day-expiry subscriber orders for that final 5 minutes.
    # Safety net so a trader who forgets to close expiring options doesn't leave
    # subscribers holding contracts that expire worthless overnight. Later-expiry
    # options and all stocks are untouched. Set false to disable BOTH halves
    # without a redeploy. The sweep loop runs in the worker only
    # (run_background_workers=true); the order lockout runs wherever fanout runs.
    eod_autoclose_enabled: bool = True
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
