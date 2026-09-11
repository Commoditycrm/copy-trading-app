import uuid
from datetime import datetime, time

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    The Discord session itself lives on ``DiscordAccount`` — it authenticates an
    ACCOUNT, not a channel, so one sign-in covers every channel that account can
    read. This row only records WHICH channel to watch and when.

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

    # Which connected Discord account reads this channel. The session lives on
    # the ACCOUNT (models/discord_account.py), so connecting once covers every
    # channel that account can see — adding a second channel never asks the
    # trader to sign in again.
    #
    # Nullable + SET NULL so removing an account leaves its channels and their
    # message history intact; they simply need an account re-attached.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discord_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Trader on/off for THIS source. The listener only opens enabled sources.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Active window ────────────────────────────────────────────────────────
    # When to actually hold a browser session open for this channel. Outside the
    # window the assignment is withheld and the listener closes the watcher, so
    # a trader following US options alerts isn't holding a live Discord session
    # at 3am. Enforced in the assignments query, NOT in the listener — see
    # services/discord_schedule.py.
    #
    # Default "always" so every source created before this existed is unchanged.
    #
    # NOTE the real trade-off: alerts posted outside the window are NOT ingested,
    # because nothing is connected to observe them.
    schedule_mode: Mapped[str] = mapped_column(
        String(16), default="always", server_default="always", nullable=False,
    )
    # Only meaningful for schedule_mode == "custom". end < start is a window that
    # crosses midnight (e.g. 22:00-06:00), which is handled explicitly.
    schedule_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    schedule_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    # IANA name (e.g. "America/New_York"). An unresolvable value falls back to
    # ET rather than taking the source offline — a typo must not silently stop
    # alerts. The "market"/"extended" modes are always ET regardless.
    schedule_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Days the window applies to, Mon=0 … Sun=6. Empty/NULL means weekdays.
    schedule_days: Mapped[list[int]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), default=list, server_default="[]", nullable=False,
    )

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

    account = relationship("DiscordAccount", back_populates="sources")
