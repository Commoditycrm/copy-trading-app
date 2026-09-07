"""Inbound Discord alert-copying — reader side (Step 1: connection only).

This module owns talking TO Discord's REST API for the INBOUND feature. For
step 1 it only VERIFIES a connection — it proves the trader's bot token is valid
and can see the channel we'll later read alerts from.

── The Follow model (why the bot lives in the trader's OWN server) ──────────────
We can't add a bot to a third-party alert server — we don't own it and the alert
provider won't grant permission. So we never touch the source server. Instead the
trader uses Discord's native **Channel Following**:

  source announcement channel  ──Follow──▶  a channel in the TRADER'S own server

Following cross-posts every published announcement into the trader's channel (via
a Discord-managed webhook). The trader owns that server, so they can freely add
OUR bot there and give it read access. We then read the *follower* channel — 100%
within Discord's ToS, no user tokens / self-bots.

So ``channel_id`` here is always the trader's OWN follower channel, not the source.

Kept entirely separate from services/discord_alerts.py, which is the OUTBOUND
webhook broadcast (posting the trader's own fills TO Discord).
"""
from __future__ import annotations

import httpx

_DISCORD_API = "https://discord.com/api/v10"
_TIMEOUT = 10.0

# Discord message flag: the message originated in another channel and arrived
# here via Channel Following (i.e. it's a cross-posted announcement). Its
# presence on recent messages is proof the Follow is live and alerts are
# flowing into this channel. https://discord.com/developers/docs/resources/message#message-object-message-flags
_FLAG_IS_CROSSPOST = 1 << 1  # 2


class DiscordVerifyError(Exception):
    """User-facing reason a bot-token / channel connection couldn't be verified."""


def _detect_followed_alerts(client: httpx.Client, headers: dict, channel_id: str) -> bool | None:
    """Best-effort: do recent messages in this channel look like followed
    announcements (cross-posted / webhook-authored)?

    Returns True if we spotted at least one, False if the channel is readable but
    no followed messages were seen recently, or None if we couldn't tell (the bot
    lacks Read Message History, or Discord hiccuped). NEVER raises — this is an
    informational hint layered on top of the hard connection check, never a gate.
    """
    try:
        resp = client.get(
            f"{_DISCORD_API}/channels/{channel_id}/messages",
            headers=headers,
            params={"limit": 25},
        )
        if resp.status_code != 200:
            return None  # e.g. 403 = no Read Message History → unknown, not a failure
        for msg in resp.json():
            flags = msg.get("flags") or 0
            if (flags & _FLAG_IS_CROSSPOST) or msg.get("webhook_id"):
                return True
        return False
    except (httpx.HTTPError, ValueError):
        return None


def verify_bot_channel(bot_token: str, channel_id: str) -> dict:
    """Validate a trader's Discord bot token AND that the bot can read the given
    (follower) channel. Returns
    ``{"channel_name", "guild_id", "channel_type", "receiving_alerts"}`` on
    success; raises ``DiscordVerifyError`` with a message safe to show the trader
    on failure.

    Hard checks (either failing raises):
      1. GET /users/@me     → the token is a real bot token (401 if not).
      2. GET /channels/{id} → the bot can see that channel (403/404 if not).

    Soft check (never raises, populates ``receiving_alerts`` = True/False/None):
      3. GET /channels/{id}/messages → are followed alerts actually arriving?
    """
    token = (bot_token or "").strip()
    if not token:
        raise DiscordVerifyError("Bot token is required.")
    headers = {"Authorization": f"Bot {token}"}
    cid = channel_id.strip()
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            me = c.get(f"{_DISCORD_API}/users/@me", headers=headers)
            if me.status_code == 401:
                raise DiscordVerifyError("Invalid bot token — Discord rejected it (401).")
            me.raise_for_status()

            ch = c.get(f"{_DISCORD_API}/channels/{cid}", headers=headers)
            if ch.status_code in (401, 403):
                raise DiscordVerifyError(
                    "The bot can't see that channel. Add the bot to YOUR server (the "
                    "one with the follower channel) and give it permission to view the "
                    "channel and read message history."
                )
            if ch.status_code == 404:
                raise DiscordVerifyError("Channel not found — double-check the follower channel ID.")
            ch.raise_for_status()
            body = ch.json()

            receiving = _detect_followed_alerts(c, headers, cid)
    except DiscordVerifyError:
        raise
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise DiscordVerifyError(f"Couldn't reach Discord to verify: {exc}") from exc

    guild = body.get("guild_id")
    return {
        "channel_name": body.get("name"),
        "guild_id": str(guild) if guild else None,
        "channel_type": body.get("type"),
        "receiving_alerts": receiving,
    }
