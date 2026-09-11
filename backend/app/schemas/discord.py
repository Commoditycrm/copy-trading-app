"""Schemas for INBOUND Discord alert sources (browser-session ingestion).

Two audiences share this module, and the split matters:

  * TRADER-facing (``DiscordSource*``) — everything a logged-in trader can see.
    NEVER carries the Discord session; only the non-sensitive
    ``DiscordSessionInfo`` summary derived from it.
  * LISTENER-facing (``DiscordAssignmentOut``, ``DiscordMessageBatchIn``,
    ``DiscordListenerStatusIn``) — the internal contract with the
    discord-listener container, reached only with the shared listener token.
    ``DiscordAssignmentOut`` is the ONE place a decrypted session crosses a wire.
"""
import uuid
from datetime import datetime, time
from typing import Any

from pydantic import BaseModel, Field


# ── Trader-facing ────────────────────────────────────────────────────────────

class DiscordSourceIn(BaseModel):
    """Connect a Discord channel to monitor.

    We take the channel URL rather than raw ids: copying the address bar works
    for every trader, whereas right-click → Copy Channel ID requires Developer
    Mode to be switched on first.
    """

    label: str = Field(min_length=1, max_length=120)
    channel_url: str = Field(
        min_length=10,
        max_length=300,
        description="https://discord.com/channels/<server>/<channel>",
    )


class DiscordSourceUpdateIn(BaseModel):
    """Partial update — any field left unset is unchanged."""

    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_enabled: bool | None = None

    # Active window. "always" | "market" | "extended" | "custom".
    schedule_mode: str | None = Field(default=None, pattern=r"^(always|market|extended|custom)$")
    schedule_start: time | None = None
    schedule_end: time | None = None
    schedule_timezone: str | None = Field(default=None, max_length=64)
    # Mon=0 … Sun=6. Empty means weekdays.
    schedule_days: list[int] | None = Field(default=None, max_length=7)

    # Repoint this source at a different channel. Allowed WITHOUT re-uploading a
    # session, because the stored session authenticates the Discord ACCOUNT, not
    # one channel — any channel that account can already open is reachable with
    # it. Without this, correcting a mistyped channel would mean deleting the
    # source, which discards the session and forces the whole sign-in again.
    channel_url: str | None = Field(default=None, min_length=10, max_length=300)


class DiscordSessionIn(BaseModel):
    """Upload of a Playwright ``storage_state`` captured by the login helper.

    The trader signs into Discord themselves in a real browser window — password
    and MFA are typed by them, into Discord, and never reach Kopyaa. What lands
    here is the resulting session, which we validate and immediately encrypt.
    """

    storage_state: dict[str, Any]


class DiscordSessionInfo(BaseModel):
    """Non-sensitive description of a stored session. Safe for the frontend:
    counts and timestamps only, never a cookie name, domain or value."""

    present: bool
    cookie_count: int
    captured_at: datetime | None = None
    age_days: int | None = None


class DiscordSourceOut(BaseModel):
    """Public view of a source. NEVER includes the Discord session itself."""

    id: uuid.UUID
    label: str
    channel_id: str
    channel_name: str | None
    guild_id: str | None
    guild_name: str | None
    is_enabled: bool
    # needs_login | connecting | connected | disconnected | error
    status: str
    last_error: str | None
    last_heartbeat_at: datetime | None
    last_message_at: datetime | None
    last_seen_message_id: str | None
    created_at: datetime

    # Active window, plus a human-readable summary for the card.
    schedule_mode: str
    schedule_start: time | None
    schedule_end: time | None
    schedule_timezone: str | None
    schedule_days: list[int]
    schedule_summary: str = "Always"

    # Derived, not a column — the API layer fills this in from the encrypted
    # session via ``describe_session``. It needs a default because
    # ``model_validate(orm_row)`` runs BEFORE that assignment and the ORM object
    # has no ``session`` attribute to read. The default is deliberately the
    # conservative one: reporting "no session" when we haven't looked is safe,
    # whereas defaulting to present=True would claim a credential exists that
    # might not.
    session: DiscordSessionInfo = DiscordSessionInfo(present=False, cookie_count=0)

    model_config = {"from_attributes": True}


# ── Listener-facing (internal, shared-token authenticated) ───────────────────

class DiscordAssignmentOut(BaseModel):
    """One channel the listener should keep open, with the session to open it.

    The only response in the app that carries a decrypted Discord session. It is
    served exclusively to the listener container over the token-authenticated
    internal route, and the listener holds it in memory for the browser context
    — it is never written to the listener's disk or logs.
    """

    source_id: uuid.UUID
    user_id: uuid.UUID
    guild_id: str | None
    channel_id: str
    label: str
    last_seen_message_id: str | None
    storage_state: dict[str, Any]


class DiscordIncomingMessage(BaseModel):
    """One message observed in the rendered channel.

    Field set is fixed by PHASE 2. ``message_id`` is Discord's own snowflake,
    read off the DOM node id — that is what makes idempotency possible, so it is
    the one required field besides content.
    """

    message_id: str = Field(min_length=1, max_length=40, pattern=r"^\d+$")
    channel_id: str = Field(min_length=1, max_length=40, pattern=r"^\d+$")
    server_id: str | None = Field(default=None, max_length=40)
    author: str | None = Field(default=None, max_length=200)
    author_id: str | None = Field(default=None, max_length=40)
    content: str = Field(default="", max_length=8000)
    timestamp: datetime | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    embeds: list[dict[str, Any]] = Field(default_factory=list)
    # True when the observer saw an existing message change rather than a new
    # one arrive. Discord edits alerts in place often enough ("filled", "closed"
    # appended to the original post) that the parser will need to know.
    is_edit: bool = False


class DiscordMessageBatchIn(BaseModel):
    """A flush from the observer. Batched because a burst of alerts renders as
    several nodes in one MutationObserver callback."""

    source_id: uuid.UUID
    messages: list[DiscordIncomingMessage] = Field(default_factory=list)


class DiscordIngestOut(BaseModel):
    """Per-batch result, so the listener can log what actually landed."""

    accepted: int
    duplicates: int
    rejected: list[dict[str, str]] = Field(default_factory=list)
    # Stored but not queued (e.g. Redis down). The row survives at RECEIVED, so
    # this is a degraded-pipeline signal, not a lost message.
    queue_failed: int = 0


class DiscordListenerStatusIn(BaseModel):
    """Connection state / heartbeat reported by the listener for one source."""

    source_id: uuid.UUID
    status: str = Field(pattern=r"^(connecting|connected|disconnected|error|needs_login)$")
    error: str | None = Field(default=None, max_length=500)
    channel_name: str | None = Field(default=None, max_length=200)
    guild_name: str | None = Field(default=None, max_length=200)
    # Where watching started, sent once on a channel's first attach. Existing
    # history below this point is deliberately never ingested.
    baseline_message_id: str | None = Field(default=None, max_length=40, pattern=r"^\d+$")


# ── Stored messages (audit trail) ────────────────────────────────────────────

class DiscordMessageOut(BaseModel):
    """One stored message, as shown in View Messages.

    Returns the message EXACTLY as it arrived — content, attachments and embeds
    untouched — because the point of the audit trail is answering "what did the
    alert actually say" after a trade went wrong, not showing a tidied version.
    """

    id: uuid.UUID
    discord_message_id: str
    discord_channel_id: str
    discord_server_id: str | None
    author: str | None
    author_id: str | None
    content: str
    # When DISCORD says it was posted. Not when we saw it: a backlog replay
    # after a restart ingests old messages now, so created_at can be much later.
    posted_at: datetime | None
    attachments: list[Any]
    embeds: list[Any]
    # received | ignored | parsed | invalid | order_created | order_failed
    status: str
    status_reason: str | None
    order_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── QR login (connecting a Discord account without a terminal) ───────────────

class DiscordDecisionOut(BaseModel):
    """Result of accepting or rejecting one alert."""

    id: uuid.UUID
    decision: str
    decided_at: datetime | None


class DiscordLoginOut(BaseModel):
    """State of a QR login attempt, polled by the frontend.

    ``qr_image`` is a data URI ready to drop into an <img>. It is a live login
    credential, so it is present only while the session is awaiting a scan and is
    cleared the moment the attempt ends.
    """

    session_id: uuid.UUID
    # pending | starting | awaiting_scan | scanned | complete | failed
    status: str
    qr_image: str | None = None
    error: str | None = None


class DiscordLoginRequestOut(BaseModel):
    """A login attempt handed to the listener. Carries no credential — the
    listener creates the browser session from scratch."""

    session_id: uuid.UUID
    source_id: uuid.UUID
    status: str


class DiscordLoginQrIn(BaseModel):
    """A freshly captured QR frame, base64 PNG (no data: prefix)."""

    qr_png: str = Field(min_length=32, max_length=4_000_000)


class DiscordLoginStatusIn(BaseModel):
    """Listener-reported progress on a login attempt."""

    status: str = Field(pattern=r"^(starting|awaiting_scan|scanned|failed)$")
    error: str | None = Field(default=None, max_length=500)


class DiscordLoginCompleteIn(BaseModel):
    """The captured Discord session, handed over once the trader has approved
    the scan on their phone. Encrypted onto the source immediately."""

    storage_state: dict[str, Any]


# ── Desktop Connector pairing ────────────────────────────────────────────────

class DiscordPairOut(BaseModel):
    """A pairing code, shown to the source's owner so they can type it into the
    Kopyaa Connector. Never carries the upload token."""

    code: str                      # display form: KPY-4F2A-9C1D
    # pending | claimed | complete | failed
    status: str
    error: str | None = None


class DiscordPairClaimIn(BaseModel):
    """The Connector redeeming a code it was given by the trader."""

    code: str = Field(min_length=6, max_length=20)


class DiscordPairClaimOut(BaseModel):
    """What the Connector needs to finish the job. ``upload_token`` is returned
    exactly once, on a successful claim, and is the only thing that authorises
    writing a session to this source."""

    source_id: uuid.UUID
    label: str
    upload_token: str


class DiscordPairCompleteIn(BaseModel):
    """The captured Discord session, handed over by the Connector."""

    code: str = Field(min_length=6, max_length=20)
    upload_token: str = Field(min_length=16, max_length=200)
    storage_state: dict[str, Any]


class DiscordSignalOut(BaseModel):
    """A parsed Discord alert, shaped for the Order History "Discord" tab.

    Display only. These are readings of what an alert SAID — no order exists
    behind them, and ``status`` reflects how far the message got in the
    pipeline, not a broker state. ``order_id`` stays null until execution is
    wired up.
    """

    # Unique per ROW. One message can carry several trades, so the message id
    # alone isn't a key.
    row_key: str
    id: uuid.UUID
    source_id: uuid.UUID
    source_label: str
    channel_name: str | None

    discord_message_id: str
    author: str | None
    posted_at: datetime | None
    created_at: datetime
    # The original text, kept alongside the reading so a wrong parse is
    # immediately obvious in the UI.
    content: str
    embeds: list[Any]

    # received | ignored | parsed | invalid | order_created | order_failed
    status: str
    status_reason: str | None

    # Where the channel stands on this alert:
    #   pending  — parsed, waiting for the trader (manual mode)
    #   approved — cleared for execution (auto mode, or accepted)
    #   rejected — the trader declined it
    #   null     — never became a signal, so there's nothing to decide
    decision: str | None = None
    decided_at: datetime | None = None
    # The mode that applied to THIS alert, which may differ from the channel's
    # current setting.
    decision_mode: str | None = None

    # Flattened from parsed_signal for the table; null when not PARSED.
    action: str | None = None
    asset_type: str | None = None
    symbol: str | None = None
    option_type: str | None = None
    strike: str | None = None
    expiration: str | None = None
    quantity: str | None = None
    order_type: str | None = None
    limit_price: str | None = None
    is_partial_close: bool = False
    remaining_quantity: str | None = None
    original_quantity: str | None = None
    position_closed: bool = False
    # The alert named a contract but no expiry (typical of exit alerts); it must
    # be resolved from the open position, never guessed.
    expiry_unspecified: bool = False
    source_action: str | None = None

    # Figures the alert REPORTED, not computed by us.
    notional: str | None = None
    pnl_amount: str | None = None
    pnl_percent: str | None = None
    total_pnl_amount: str | None = None
    total_pnl_percent: str | None = None

    order_id: uuid.UUID | None = None


class DiscordSettingsOut(BaseModel):
    """Account-wide handling of inbound Discord alerts."""

    # "manual" — accept or reject each alert in Order History
    # "auto"   — a successful parse is approved immediately
    execution_mode: str


class DiscordSettingsIn(BaseModel):
    execution_mode: str = Field(pattern=r"^(auto|manual)$")
