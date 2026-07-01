"""Test helpers: direct user creation + token minting for the auth suite.

These bypass the HTTP layer so a test can arrange exact preconditions (an
inactive user, an already-verified user, a fresh reset token) without depending
on the very endpoints under test.
"""
import uuid
from decimal import Decimal

from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    hash_password,
)
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole

DEFAULT_PW = "Str0ng!pw"  # 8+ chars, meets the 3-of-4 class policy


def make_user(
    db,
    email: str,
    *,
    password: str = DEFAULT_PW,
    role: UserRole = UserRole.SUBSCRIBER,
    is_active: bool = True,
    email_verified: bool = False,
    business_name: str | None = None,
    display_name: str | None = None,
) -> User:
    """Create + persist a user with the matching settings row, mirroring what
    register() does, so foreign-key-dependent flows behave realistically."""
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
        email_verified=email_verified,
        business_name=business_name,
        display_name=display_name,
    )
    db.add(user)
    db.flush()
    if role == UserRole.TRADER:
        db.add(TraderSettings(user_id=user.id, trading_enabled=True))
    elif role == UserRole.SUBSCRIBER:
        db.add(SubscriberSettings(user_id=user.id, copy_enabled=False, multiplier=Decimal("1.000")))
    db.commit()
    db.refresh(user)
    return user


def reset_token_for(user: User) -> str:
    return create_password_reset_token(str(user.id), user.password_hash)


def verify_token_for(user: User) -> str:
    return create_email_verification_token(str(user.id), user.email)


def access_token_for(user: User) -> str:
    return create_access_token(str(user.id), user.role.value)


def refresh_token_for(user: User) -> str:
    return create_refresh_token(str(user.id))


def auth_header(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token_for(user)}"}


# --- fresh-connection readers (see committed state written by the endpoint) ---
from sqlalchemy import text  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402


def audit_actions() -> list[str]:
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT action FROM audit_logs"))]


def fetch_user(email: str) -> User | None:
    with SessionLocal() as s:
        u = s.query(User).filter(User.email == email.strip().lower()).first()
        if u is not None:
            s.expunge(u)
        return u
