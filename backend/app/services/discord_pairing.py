"""Pairing codes that let the Kopyaa Connector desktop app attach a Discord
session to a source.

Why this exists
---------------
Capturing a Discord session server-side means an automated browser sitting on
Discord's login page, which Discord challenges with a CAPTCHA on its own
schedule (observed anywhere from 20 to 60 seconds). We won't solve or evade that
challenge, so server-side login can't be the onboarding path.

The Connector moves the capture to the trader's own machine: a real browser, on
a residential IP, driven by a human who is typing and clicking. If Discord does
challenge it, the trader is sitting right there and answers it themselves —
which is what CAPTCHAs are for.

What a pairing code is for
--------------------------
The Connector is a separate program with no Kopyaa login. It needs to know which
source to attach a session to, and prove it's allowed to. The code does both:

    Kopyaa UI  ──create()──▶  KPY-4F2A-9C1D   (shown only to the signed-in owner)
    Connector  ──claim()───▶  {source_id, upload_token}
    Connector  ──complete()▶  the captured session, encrypted onto the source

Single-use claim, short TTL, high-entropy alphabet. Once claimed, a second
attempt is refused — so a code glimpsed over someone's shoulder can't be raced,
and the ``upload_token`` returned on claim is what actually authorises the write.

Redis rather than a table for the same reason as ``discord_login``: this is
short-lived credential material and it should expire on its own rather than
being persisted and backed up.
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

_KEY = "discord:pair:"

# Ten minutes: long enough to download/open the Connector and sign in, short
# enough that an abandoned code stops being useful quickly.
TTL_S = 600

# Crockford-style alphabet — no 0/O, 1/I/L, U. The trader reads this off one
# screen and types it into another, so confusable characters are a real cost.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LEN = 8

PENDING = "pending"      # created, Connector hasn't claimed it
CLAIMED = "claimed"      # Connector holds it; trader is signing in to Discord
COMPLETE = "complete"    # session captured and stored
FAILED = "failed"


def _key(code: str) -> str:
    return f"{_KEY}{code.upper()}"


def normalise(code: str) -> str:
    """Accept what a human actually types: spaces, dashes, lowercase, and the
    ``KPY-`` prefix we display for recognisability."""
    cleaned = (code or "").strip().upper().replace(" ", "").replace("-", "")
    if cleaned.startswith("KPY"):
        cleaned = cleaned[3:]
    return cleaned


def format_code(code: str) -> str:
    """Display form: KPY-4F2A-9C1D."""
    return f"KPY-{code[:4]}-{code[4:]}"


def _read(code: str) -> dict[str, Any] | None:
    try:
        raw = get_sync_redis().get(_key(code))
    except Exception:  # noqa: BLE001
        log.warning("discord_pairing: read failed", exc_info=True)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _write(session: dict[str, Any]) -> None:
    try:
        get_sync_redis().setex(_key(session["code"]), TTL_S, json.dumps(session))
    except Exception:  # noqa: BLE001
        log.warning("discord_pairing: write failed", exc_info=True)


def create(source_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any]:
    """Mint a pairing code for one source. Shown only to its owner."""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    session = {
        "code": code,
        "source_id": str(source_id),
        "user_id": str(user_id),
        "status": PENDING,
        # Minted now but only ever returned once, on claim.
        "upload_token": secrets.token_urlsafe(32),
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(session)
    return session


def get(code: str) -> dict[str, Any] | None:
    return _read(normalise(code))


def claim(code: str) -> dict[str, Any] | None:
    """Redeem a code, once. Returns the session (including ``upload_token``) or
    None if the code is unknown, expired, or already used.

    Single-use is the point: it means a code seen by someone else is worthless
    the moment the real Connector has used it, and two Connectors can't both
    believe they own the same source.
    """
    session = _read(normalise(code))
    if session is None or session["status"] != PENDING:
        return None
    session["status"] = CLAIMED
    _write(session)
    log.info("discord_pairing: code claimed for source=%s", session["source_id"])
    return session


def authorise(code: str, upload_token: str) -> dict[str, Any] | None:
    """Verify a Connector's token for this code before accepting an upload.

    Compared with ``compare_digest`` so a wrong token can't be recovered by
    timing the response.
    """
    session = _read(normalise(code))
    if session is None or session["status"] != CLAIMED:
        return None
    if not secrets.compare_digest(session.get("upload_token") or "", upload_token or ""):
        return None
    return session


def finish(code: str, *, error: str | None = None) -> dict[str, Any] | None:
    """Close a pairing out. Drops the upload token — once the session is stored
    there is no reason to keep a credential that can write to this source."""
    session = _read(normalise(code))
    if session is None:
        return None
    session["status"] = FAILED if error else COMPLETE
    session["error"] = error
    session["upload_token"] = None
    _write(session)
    return session


__all__ = [
    "CLAIMED", "COMPLETE", "FAILED", "PENDING", "TTL_S",
    "authorise", "claim", "create", "finish", "format_code", "get", "normalise",
]
