import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.database import get_db
from app.models.user import User, UserRole

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def client_ip(request: Request) -> str | None:
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


def require_subscriber(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.SUBSCRIBER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="subscriber_only")
    return user
