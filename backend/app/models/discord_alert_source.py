import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DiscordAlertSource(Base, TimestampMixin):
    """A Discord channel that Kopyaa READS trade alerts FROM (INBOUND
    alert-copying), monitored through an authenticated Discord Web session.

    ── Ingestion model: browser session, not a bot ──────────────────────────────
    Step 1 of this feature connected a channel with the trader's own BOT token
    plus Discord's Channel-Following. That model is replaced here: a bot can only
    read a channel it was invited to, which rules out the third-party alert
    servers traders actually subscribe to, and the Follow workaround required
    them to own a server and wire up cross-posting.

    Instead we monitor Discord Web as the trader's own logged-in account, reading
    only channels that account can already legitimately open. No Discord
    authentication, permission, MFA or rate-limit mechanism is bypassed — we
    observe rendered messages in a session the trader established themselves.

    ``encrypted_session`` holds the Fernet-encrypted Playwright storage state
    (cookies + localStorage) captured during a one-time headed login the trader
    performs on their OWN machine — Kopyaa never sees their password or MFA code.
    Same encryption-at-rest treatment as broker credentials
    (``broker_account.encrypted_credentials``); it is NEVER returned to the
    frontend or written to logs.

    Deliberately SEPARATE from the OUTBOUND webhook broadcast
    (``TraderSettings.discord_webhook_url`` / ``discord_alerts_enabled``, which
    posts the trader's own fills TO Discord). Different direction, different
    lifecycle, different data — they must not share storage.
    """

    __tablename__ = "discord_alert_sources"
    __table_args__ = (
        # One source per (user, channel). Re-adding a channel a trader already
        # watches would double every alert into the pipeline — two sources means
        # two ingest rows for one Discord message, and the per-source
        # idempotency key can't see across them.
        UniqueConstraint("user_id", "channel_id", name="uq_discord_source_user_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)

    # Discord identifiers, parsed from the channel URL the trader pastes
    # (https://discord.com/channels/<guild_id>/<channel_id>). Names are display
    # only and are backfilled by the listener once it has the channel open.
    guild_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    guild_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Fernet-encrypted Playwright storage_state JSON for the trader's Discord
    # Web session. NULL until the one-time headed login is completed, which is
    # why status starts at "needs_login". Never leaves the backend except to the
    # listener service over its authenticated internal endpoint.
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Trader on/off for THIS source. The listener only opens enabled sources.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Connection lifecycle, written by the listener service:
    #   needs_login  — no session stored yet (or it expired / was revoked)
    #   connecting   — browser context starting, channel not yet confirmed open
    #   connected    — channel open, MutationObserver attached, heartbeats flowing
    #   disconnected — cleanly stopped (source disabled, or graceful shutdown)
    #   error        — failed to open or stay on the channel; see last_error
    status: Mapped[str] = mapped_column(String(20), default="needs_login", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Liveness + observability for the Sources UI. last_heartbeat_at proves the
    # watcher is alive even on a quiet channel (where last_message_at goes stale
    # for legitimate reasons); last_seen_message_id is the newest Discord
    # snowflake we ingested, used to resume without re-reading the backlog.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_message_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
