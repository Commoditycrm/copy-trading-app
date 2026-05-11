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
