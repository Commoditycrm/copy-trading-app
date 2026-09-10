"""QR-code login sessions for connecting a trader's Discord account.

Why QR
------
Discord exposes no OAuth scope for reading channel messages as a user, so the
only way to watch a channel is a real, authenticated browser session. That leaves
the question of how a trader hands us one without a terminal.

QR login is the answer that keeps credentials out of Kopyaa entirely: our
headless browser opens Discord's own login page, we stream the QR it renders to
the trader, and they scan it with the Discord mobile app and approve on their
phone. Authentication happens between their phone and Discord. No password, no
MFA code, and no keystroke ever passes through Kopyaa — which is exactly why this
was chosen over streaming a remote browser the trader types into.

Nothing here bypasses Discord authentication: this IS Discord's own documented
login flow, driven by the account holder on their own device.

Why Redis and not a table
-------------------------
A live QR is a scannable login credential with a ~2 minute life. Persisting it in
Postgres would mean writing short-lived credentials into durable storage and
backups for no benefit. Session state lives in Redis under a TTL and disappears
on its own; the only thing that reaches the database is the final encrypted
session, via the source's normal ``encrypted_session`` column.

Flow (all polling — the listener has no inbound port)
-----------------------------------------------------
    trader clicks Connect
      -> create()                         status=pending
    listener polls pending()              picks up the request
      -> opens discord.com/login, screenshots the QR
      -> set_qr()                         status=awaiting_scan
    trader scans with the Discord app, approves on their phone
    listener sees the app shell load, captures storage_state
      -> complete() (API layer encrypts it onto the source)
                                          status=complete
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

_KEY = "discord:login:"
_PENDING_SET = "discord:login:pending"
_COOLDOWN_KEY = "discord:login:cooldown:"

# Minimum gap between login attempts for one source.
#
# Repeatedly opening Discord's login page is what gets a browser served an
# anti-bot challenge instead of a QR — we tripped exactly that during testing by
# firing six attempts in a few minutes. Discord is entitled to rate-limit
# automated traffic, and the correct response is to stop generating it, not to
# disguise it. A retry that lands inside this window is refused up-front so the
# trader gets an honest "wait a moment" instead of a browser that silently burns
# five minutes against a CAPTCHA.
COOLDOWN_S = 60

# Long enough for a trader to find their phone, unlock it, open Discord and
# scan - but this is a login credential, so it is not open-ended. Discord's own
# QR rotates roughly every 2 minutes and the listener re-captures in place, so
# this bounds the whole attempt rather than a single QR.
TTL_S = 600

# Lifecycle. Terminal states are complete / failed.
PENDING = "pending"              # created, listener hasn't picked it up
STARTING = "starting"            # listener is opening Discord's login page
AWAITING_SCAN = "awaiting_scan"  # QR is live and being shown to the trader
SCANNED = "scanned"              # phone scanned it; waiting for them to approve
COMPLETE = "complete"            # session captured and stored
FAILED = "failed"


def _key(session_id: uuid.UUID | str) -> str:
    return f"{_KEY}{session_id}"


def _read(session_id: uuid.UUID | str) -> dict[str, Any] | None:
    try:
        raw = get_sync_redis().get(_key(session_id))
    except Exception:  # noqa: BLE001
        log.warning("discord_login: read failed for %s", session_id, exc_info=True)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _write(session: dict[str, Any]) -> None:
    """Persist with a refreshed TTL. Best-effort: losing a login session just
    means the trader retries, so this never raises into a request handler."""
    try:
        r = get_sync_redis()
        r.setex(_key(session["session_id"]), TTL_S, json.dumps(session))
        if session["status"] in (COMPLETE, FAILED):
            r.srem(_PENDING_SET, session["session_id"])
    except Exception:  # noqa: BLE001
        log.warning(
            "discord_login: write failed for %s", session.get("session_id"), exc_info=True
        )


def cooldown_remaining(source_id: uuid.UUID) -> int:
    """Seconds left before another login attempt may be started, 0 if clear."""
    try:
        ttl = get_sync_redis().ttl(f"{_COOLDOWN_KEY}{source_id}")
    except Exception:  # noqa: BLE001
        return 0
    return max(0, ttl) if ttl and ttl > 0 else 0


def create(source_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any]:
    """Open a login attempt for one source. Returns the session."""
    session_id = str(uuid.uuid4())
    try:
        get_sync_redis().setex(f"{_COOLDOWN_KEY}{source_id}", COOLDOWN_S, "1")
    except Exception:  # noqa: BLE001
        log.warning("discord_login: could not set cooldown for %s", source_id, exc_info=True)
    session = {
        "session_id": session_id,
        "source_id": str(source_id),
        "user_id": str(user_id),
        "status": PENDING,
        "qr_png": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(session)
    try:
        r = get_sync_redis()
        r.sadd(_PENDING_SET, session_id)
        # The set has no per-member TTL, so bound the set itself; stale ids are
        # also pruned on read in pending().
        r.expire(_PENDING_SET, TTL_S)
    except Exception:  # noqa: BLE001
        log.warning("discord_login: could not enqueue %s", session_id, exc_info=True)
    return session


def get(session_id: uuid.UUID | str) -> dict[str, Any] | None:
    """Fetch a session, or None once it has expired out of Redis."""
    return _read(session_id)


def pending() -> list[dict[str, Any]]:
    """Login attempts the listener should act on.

    Prunes ids whose session has already expired, so the set can't accumulate
    work that no longer exists.
    """
    try:
        r = get_sync_redis()
        ids = list(r.smembers(_PENDING_SET))
    except Exception:  # noqa: BLE001
        log.warning("discord_login: pending() unavailable", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for sid in ids:
        session = _read(sid)
        if session is None:
            try:
                r.srem(_PENDING_SET, sid)
            except Exception:  # noqa: BLE001
                pass
            continue
        if session["status"] in (PENDING, STARTING, AWAITING_SCAN, SCANNED):
            out.append(session)
    return out


def set_status(
    session_id: uuid.UUID | str, status: str, *, error: str | None = None
) -> dict[str, Any] | None:
    session = _read(session_id)
    if session is None:
        return None
    session["status"] = status
    session["error"] = error
    _write(session)
    return session


def set_qr(session_id: uuid.UUID | str, qr_png_b64: str) -> dict[str, Any] | None:
    """Store the current QR frame and mark the session ready to scan.

    Discord rotates the QR every couple of minutes; the listener re-captures and
    calls this again, so the trader's view refreshes instead of going stale.
    """
    session = _read(session_id)
    if session is None:
        return None
    session["qr_png"] = qr_png_b64
    session["status"] = AWAITING_SCAN
    session["error"] = None
    _write(session)
    return session


def finish(session_id: uuid.UUID | str, *, error: str | None = None) -> dict[str, Any] | None:
    """Close a session out. Drops the QR image on the way through - once the
    attempt is over there is no reason to keep a scannable credential around,
    even for the few minutes until the TTL would have removed it."""
    session = _read(session_id)
    if session is None:
        return None
    session["status"] = FAILED if error else COMPLETE
    session["error"] = error
    session["qr_png"] = None
    _write(session)
    return session


__all__ = [
    "COOLDOWN_S", "cooldown_remaining",
    "AWAITING_SCAN", "COMPLETE", "FAILED", "PENDING", "SCANNED", "STARTING", "TTL_S",
    "create", "finish", "get", "pending", "set_qr", "set_status",
]
