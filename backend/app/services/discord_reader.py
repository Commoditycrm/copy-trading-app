"""Inbound Discord alert-copying — reader side (Step 1: connection only).

This module owns talking TO Discord's REST API for the INBOUND feature (reading
a trader's channel). For step 1 it only VERIFIES a connection — proves the
trader's bot token is valid and can see the channel. Message ingestion, parsing,
and order placement are later phases and will be added here / alongside.

Kept entirely separate from services/discord_alerts.py, which is the OUTBOUND
webhook broadcast (posting the trader's own fills TO Discord).
"""
from __future__ import annotations

import httpx

_DISCORD_API = "https://discord.com/api/v10"
_TIMEOUT = 10.0


class DiscordVerifyError(Exception):
    """User-facing reason a bot-token / channel connection couldn't be verified."""


def verify_bot_channel(bot_token: str, channel_id: str) -> dict:
    """Validate a trader's Discord bot token AND that the bot can access the
    given channel. Returns ``{"channel_name", "guild_id"}`` on success; raises
    ``DiscordVerifyError`` with a message safe to show the trader on failure.

    Two read-only checks — no messages are fetched:
      1. GET /users/@me     → the token is a real bot token (401 if not).
      2. GET /channels/{id} → the bot can see that channel (403/404 if not).
    """
    token = (bot_token or "").strip()
    if not token:
        raise DiscordVerifyError("Bot token is required.")
    headers = {"Authorization": f"Bot {token}"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            me = c.get(f"{_DISCORD_API}/users/@me", headers=headers)
            if me.status_code == 401:
                raise DiscordVerifyError("Invalid bot token — Discord rejected it (401).")
            me.raise_for_status()

            ch = c.get(f"{_DISCORD_API}/channels/{channel_id.strip()}", headers=headers)
            if ch.status_code in (401, 403):
                raise DiscordVerifyError(
                    "The bot can't access that channel. Add the bot to the server "
                    "and give it permission to view the channel."
                )
            if ch.status_code == 404:
                raise DiscordVerifyError("Channel not found — double-check the channel ID.")
            ch.raise_for_status()
            body = ch.json()
    except DiscordVerifyError:
        raise
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise DiscordVerifyError(f"Couldn't reach Discord to verify: {exc}") from exc

    guild = body.get("guild_id")
    return {
        "channel_name": body.get("name"),
        "guild_id": str(guild) if guild else None,
    }
