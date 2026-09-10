"""Discord Web session handling for INBOUND alert-copying.

The listener monitors Discord as the trader's OWN logged-in account. The trader
performs a one-time headed login on their own machine (see the ``discord-listener``
service's ``login`` CLI); Playwright exports the resulting ``storage_state``
(cookies + localStorage) and uploads it here. Kopyaa never sees their password,
their MFA code, or their Discord account credentials, and nothing in this module
authenticates to Discord — it only stores and hands back a session the trader
already established.

Security posture (PHASE 13): the storage state is a bearer credential for that
Discord account, so it is treated exactly like a broker credential —
Fernet-encrypted at rest via ``services.crypto``, never returned to the frontend,
never logged. The only reader is the listener service over its token-authenticated
internal endpoint. ``describe_session`` exists so the API can show the trader
something meaningful ("12 cookies, captured 3 days ago") without any of it
crossing the wire.

Replaces the step-1 bot-token reader (``discord_reader.py``, deleted): a bot can
only read channels it was invited to, which is never true of the third-party
alert servers traders actually follow.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.services.crypto import decrypt_json, encrypt_json

# https://discord.com/channels/<guild_id>/<channel_id>
# The guild segment is "@me" for DMs and group DMs, which have no guild.
_CHANNEL_URL_RE = re.compile(
    r"^https?://(?:\w+\.)?discord(?:app)?\.com/channels/(?P<guild>@me|\d{1,30})/(?P<channel>\d{1,30})"
)
_SNOWFLAKE_RE = re.compile(r"^\d{1,30}$")

# Domains whose cookies actually constitute a Discord login. A storage state
# scraped from some other site would be structurally valid but useless, and we
# want that rejected at upload time rather than as a mystery listener failure.
_DISCORD_COOKIE_DOMAINS = ("discord.com", "discordapp.com")


class DiscordSessionError(Exception):
    """User-facing reason a channel reference or session upload was rejected."""


def parse_channel_url(url: str) -> tuple[str | None, str]:
    """Parse a Discord Web channel URL into ``(guild_id, channel_id)``.

    ``guild_id`` is None for DM / group-DM channels (the "@me" pseudo-guild).
    Raises :class:`DiscordSessionError` on anything that isn't a channel URL —
    we take the URL rather than raw ids because copying the address bar is the
    one action a trader can perform without developer mode enabled.
    """
    m = _CHANNEL_URL_RE.match((url or "").strip())
    if not m:
        raise DiscordSessionError(
            "That doesn't look like a Discord channel link. Open the channel in "
            "Discord and copy the address, e.g. "
            "https://discord.com/channels/123456789/987654321"
        )
    guild = m.group("guild")
    return (None if guild == "@me" else guild), m.group("channel")


def is_snowflake(value: str) -> bool:
    """True if ``value`` is shaped like a Discord id (a numeric snowflake)."""
    return bool(_SNOWFLAKE_RE.match((value or "").strip()))


def validate_storage_state(raw: Any) -> dict[str, Any]:
    """Check an uploaded Playwright ``storage_state`` before we encrypt it.

    Structural only — we cannot tell from here whether the session is still
    valid with Discord (only the listener can, by opening the channel). What we
    can do is reject payloads that are obviously not a Discord session, so the
    trader gets a clear error at upload time instead of a stuck listener.

    Never raises with any part of the payload in the message: cookie values are
    the credential itself.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise DiscordSessionError("Session file isn't valid JSON.") from exc
    if not isinstance(raw, dict):
        raise DiscordSessionError("Session file must be a Playwright storage_state object.")

    cookies = raw.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise DiscordSessionError(
            "Session file has no cookies — re-run the Discord login helper and "
            "make sure you finish signing in before closing the browser."
        )
    if not all(isinstance(c, dict) for c in cookies):
        raise DiscordSessionError("Session file has a malformed cookies array.")

    domains = {str(c.get("domain", "")).lstrip(".").lower() for c in cookies}
    if not any(d.endswith(_DISCORD_COOKIE_DOMAINS) for d in domains):
        raise DiscordSessionError(
            "That session isn't for Discord — no discord.com cookies in the file."
        )

    origins = raw.get("origins")
    if origins is not None and not isinstance(origins, list):
        raise DiscordSessionError("Session file has a malformed origins array.")

    # Store only the two keys Playwright consumes. Anything else the exporter
    # tacked on is dropped rather than persisted into our encrypted blob.
    return {"cookies": cookies, "origins": origins or []}


def encrypt_session(state: dict[str, Any]) -> str:
    """Fernet-encrypt a validated storage state for storage at rest."""
    return encrypt_json(state)


def decrypt_session(token: str) -> dict[str, Any]:
    """Decrypt a stored storage state. Raises ValueError('credential_decrypt_failed')
    via ``crypto.decrypt_json`` if the encryption key has rotated."""
    return decrypt_json(token)


def describe_session(token: str | None, captured_at: datetime | None) -> dict[str, Any]:
    """Non-sensitive summary of a stored session, safe to return to the frontend.

    Deliberately returns counts and timestamps only — never a cookie name,
    domain or value. Callers use this to render "Session active · captured 2
    days ago" without the credential ever leaving the backend.
    """
    if not token:
        return {"present": False, "cookie_count": 0, "captured_at": None, "age_days": None}
    try:
        state = decrypt_session(token)
        count = len(state.get("cookies") or [])
    except (ValueError, TypeError):
        # A session we can't decrypt is, for every practical purpose, absent —
        # the listener will fail to use it and the trader must re-login.
        return {"present": False, "cookie_count": 0, "captured_at": captured_at, "age_days": None}
    age = None
    if captured_at is not None:
        ref = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=timezone.utc)
        age = max(0, (datetime.now(timezone.utc) - ref).days)
    return {
        "present": True,
        "cookie_count": count,
        "captured_at": captured_at,
        "age_days": age,
    }


__all__ = [
    "DiscordSessionError",
    "decrypt_session",
    "describe_session",
    "encrypt_session",
    "is_snowflake",
    "parse_channel_url",
    "validate_storage_state",
]
