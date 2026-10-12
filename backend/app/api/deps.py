import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.database import get_db
from app.models.user import User, UserRole

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def client_ip(request: Request | None) -> str | None:
    # None when the caller is a background worker rather than a request — an
    # auto-trim, a poller-driven close. There is no client, and an audit row
    # saying so is better than one the worker cannot write at all.
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def current_user(
    token: str | None = Depends(_oauth2), db: Session = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing_token")
    try:
        payload = decode_token(token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong_token_type")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    user = db.get(User, uuid.UUID(sub))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user_inactive")
    return user


def require_trader(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.TRADER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="trader_only")
    return user


def sell_all_allowed(db: Session, user: User) -> bool:
    """Who gets the Sell-All / Snapshot / Re-entry suite.

    Two ways in, and ONE definition of them so the API gate and the /me payload
    the UI hides buttons on can never disagree:

      * the admin allow-list (``users.sell_all_access``), toggled per user in
        Admin -> Traders / Admin -> Users; or
      * being a subscriber of a Discord-enabled trader. Those subscribers trade
        the trader's channels on their OWN settings, and the trader cannot act
        on their positions (the subscriber-targeting bulk actions refuse, and
        the bar hides them) — so exiting has to be theirs to do, without an
        admin step.

    Never grants to an admin or any other role: the suite acts on the caller's
    own broker positions, which those do not have.
    """
    if user.role not in (UserRole.TRADER, UserRole.SUBSCRIBER):
        return False
    if getattr(user, "sell_all_access", False):
        return True
    from app.services import discord_subscribers  # noqa: PLC0415

    return discord_subscribers.followed_discord_trader(db, user) is not None


def require_sell_all_access(
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> User:
    """Gate the Sell-All / Snapshot / Re-entry suite — see sell_all_allowed.

    The subscriber-TARGETING bulk actions (close/cancel across subscribers)
    stay trader-only via `require_trader` on those endpoints.
    """
    if user.role not in (UserRole.TRADER, UserRole.SUBSCRIBER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="trader_or_subscriber_only")
    if not sell_all_allowed(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="sell_all_access_required")
    return user


def require_subscriber(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.SUBSCRIBER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="subscriber_only")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_only")
    return user
