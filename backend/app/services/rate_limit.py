"""Lightweight Redis-backed fixed-window rate limiting for auth endpoints.

Brute-force protection for /api/auth/login (and a light cap on /register).
Counters live in Redis with a TTL equal to their window, so they reset
automatically — no sweeper needed.

Fail-open by design: if Redis is unreachable we ALLOW the request. The
real gate is still the bcrypt password check; we must not lock everyone
out of the app just because the cache tier is down. Failures are logged.

Keying
------
- Per-account lockout is keyed on the (normalized) email and counts only
  FAILED logins. This is the brute-force gate and it can't be bypassed by
  spoofing X-Forwarded-For, since email isn't attacker-chosen for a given
  victim. Time-bounded so it self-heals (limits account-lockout DoS).
- Per-IP throttle counts ALL attempts and bounds automated sweeps from a
  single source. IP can be spoofed via XFF behind a misconfigured proxy,
  so it's a secondary defence layer, never the only one.
"""
from __future__ import annotations

import logging

from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

# Per-email failed-login lockout.
_FAIL_EMAIL_LIMIT = 8
_FAIL_EMAIL_WINDOW_S = 900  # 15 minutes

# Per-IP attempt throttle (login).
_LOGIN_IP_LIMIT = 40
_LOGIN_IP_WINDOW_S = 900

# Per-IP registration throttle (limits mass account creation / enumeration).
_REGISTER_IP_LIMIT = 15
_REGISTER_IP_WINDOW_S = 3600

# Email-change requests, per user — each one emails a confirmation to an
# arbitrary address, so an unbounded endpoint is an email-bombing vector.
_EMAIL_CHANGE_LIMIT = 5
_EMAIL_CHANGE_WINDOW_S = 3600  # 1 hour

# Surfaced in the Retry-After header on a 429.
RETRY_AFTER_S = _FAIL_EMAIL_WINDOW_S


def _incr_with_ttl(key: str, window_seconds: int) -> int | None:
    """INCR `key`, setting its TTL on first touch. Returns the new count,
    or None if Redis is unavailable (caller should fail open)."""
    try:
        r = get_sync_redis()
        count = int(r.incr(key))
        if count == 1:
            r.expire(key, window_seconds)
        return count
    except Exception:  # noqa: BLE001
        log.warning("rate_limit: redis unavailable (fail-open) key=%s", key, exc_info=True)
        return None


def _get_count(key: str) -> int:
    try:
        v = get_sync_redis().get(key)
        return int(v) if v is not None else 0
    except Exception:  # noqa: BLE001
        log.warning("rate_limit: redis read failed (fail-open) key=%s", key, exc_info=True)
        return 0


def _reset(key: str) -> None:
    try:
        get_sync_redis().delete(key)
    except Exception:  # noqa: BLE001
        log.warning("rate_limit: redis delete failed key=%s", key, exc_info=True)


def _fail_key(email: str) -> str:
    return f"rl:login:fail:{email.strip().lower()}"


def login_locked(email: str) -> bool:
    """True if this account has hit the failed-login limit (read-only)."""
    return _get_count(_fail_key(email)) >= _FAIL_EMAIL_LIMIT


def login_ip_throttled(ip: str | None) -> bool:
    """Count this login attempt against the per-IP budget; True if over."""
    if not ip:
        return False
    count = _incr_with_ttl(f"rl:login:ip:{ip}", _LOGIN_IP_WINDOW_S)
    return count is not None and count > _LOGIN_IP_LIMIT


def record_login_failure(email: str) -> None:
    _incr_with_ttl(_fail_key(email), _FAIL_EMAIL_WINDOW_S)


def reset_login_failures(email: str) -> None:
    """Clear the failed-login counter after a successful authentication."""
    _reset(_fail_key(email))


def register_ip_throttled(ip: str | None) -> bool:
    """Count this registration against the per-IP budget; True if over."""
    if not ip:
        return False
    count = _incr_with_ttl(f"rl:register:ip:{ip}", _REGISTER_IP_WINDOW_S)
    return count is not None and count > _REGISTER_IP_LIMIT


def email_change_throttled(user_id: str) -> bool:
    """Count this email-change request against the per-user budget; True if
    over. Caps how fast one account can fire confirmation emails at arbitrary
    addresses."""
    count = _incr_with_ttl(f"rl:email_change:{user_id}", _EMAIL_CHANGE_WINDOW_S)
    return count is not None and count > _EMAIL_CHANGE_LIMIT
