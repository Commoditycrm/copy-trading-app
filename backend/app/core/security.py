import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def _encode(payload: dict[str, Any], expires_delta: timedelta) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    to_encode = {**payload, "iat": now, "exp": now + expires_delta}
    return jwt.encode(to_encode, s.jwt_secret, algorithm=s.jwt_algorithm)


def create_access_token(subject: str, role: str) -> str:
    s = get_settings()
    return _encode(
        {"sub": subject, "role": role, "type": "access"},
        timedelta(minutes=s.jwt_access_token_minutes),
    )


def create_refresh_token(subject: str) -> str:
    s = get_settings()
    return _encode({"sub": subject, "type": "refresh"}, timedelta(days=s.jwt_refresh_token_days))


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("invalid_token") from exc


# ── Password-reset tokens ────────────────────────────────────────────────────
#
# A reset token is a short-lived JWT (type="reset") whose ``pwf`` claim is an
# HMAC fingerprint of the user's CURRENT password hash. Because the fingerprint
# is checked against the live hash at reset time, the token becomes invalid the
# instant the password changes — which makes it effectively single-use (using
# it changes the hash) and also invalidates any outstanding reset links if the
# user resets via another route. No DB table / migration needed.


def _password_fingerprint(password_hash: str) -> str:
    """Stable, non-reversible fingerprint of a password hash, keyed by the
    JWT secret. Truncated — collision risk is irrelevant since it's only ever
    compared against the same user's current hash."""
    s = get_settings()
    return hmac.new(
        s.jwt_secret.encode(), password_hash.encode(), hashlib.sha256
    ).hexdigest()[:16]


def create_password_reset_token(subject: str, password_hash: str) -> str:
    s = get_settings()
    return _encode(
        {"sub": subject, "type": "reset", "pwf": _password_fingerprint(password_hash)},
        timedelta(minutes=s.password_reset_token_minutes),
    )


def decode_password_reset_token(token: str) -> dict[str, Any]:
    """Decode + validate a reset token's shape. Raises ValueError on a bad,
    expired, or wrong-type token. Does NOT check the password fingerprint —
    the caller does that against the live user row (it needs DB access)."""
    claims = decode_token(token)
    if claims.get("type") != "reset":
        raise ValueError("wrong_token_type")
    return claims


def password_fingerprint_matches(token_claims: dict[str, Any], password_hash: str) -> bool:
    """True if the reset token was minted against this exact password hash."""
    return hmac.compare_digest(
        str(token_claims.get("pwf", "")), _password_fingerprint(password_hash)
    )


# ── Email-verification tokens ────────────────────────────────────────────────
#
# A verification token is a short-lived JWT (type="verify") that carries the
# email it was issued for in the ``eml`` claim. Confirming the token marks that
# address verified; binding to the email means a link goes stale if the user
# later changes their email.


def create_email_verification_token(subject: str, email: str) -> str:
    s = get_settings()
    return _encode(
        {"sub": subject, "type": "verify", "eml": email},
        timedelta(minutes=s.email_verification_token_minutes),
    )


def decode_email_verification_token(token: str) -> dict[str, Any]:
    """Decode + validate a verification token's shape. Raises ValueError on a
    bad, expired, or wrong-type token."""
    claims = decode_token(token)
    if claims.get("type") != "verify":
        raise ValueError("wrong_token_type")
    return claims


# ── Email-change tokens ──────────────────────────────────────────────────────
#
# A change token (type="email_change") carries the NEW address (``new``) plus
# the account's CURRENT email at request time (``cur``). At confirm time the
# caller re-checks ``cur`` against the live email, so the instant the email
# actually changes any outstanding change tokens go stale — the same "bind to
# mutable state" invalidation the reset token gets from its password fingerprint.


def create_email_change_token(subject: str, new_email: str, current_email: str) -> str:
    s = get_settings()
    return _encode(
        {"sub": subject, "type": "email_change", "new": new_email, "cur": current_email},
        timedelta(minutes=s.email_verification_token_minutes),
    )


def decode_email_change_token(token: str) -> dict[str, Any]:
    """Decode + validate a change token's shape. Raises ValueError on a bad,
    expired, or wrong-type token. The ``cur`` claim must still be re-checked
    against the live user row by the caller."""
    claims = decode_token(token)
    if claims.get("type") != "email_change":
        raise ValueError("wrong_token_type")
    return claims
