from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.auth import LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut
from app.services import audit, rate_limit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)) -> User:
    if rate_limit.register_ip_throttled(client_ip(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too_many_requests",
            headers={"Retry-After": str(rate_limit.RETRY_AFTER_S)},
        )
    existing = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email_taken")

    # Multi-trader is now supported — anyone can register as TRADER. Each
    # trader is independent: their own broker_accounts, their own copy
    # fanout, their own subscribers list. SubscriberSettings.following_trader_id
    # is a free FK to any user with role=TRADER, so subscribers pick whichever
    # trader they want from the Following dropdown.
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name,
        # business_name is required for traders (enforced by RegisterIn) and
        # forced to None for everyone else, so this is safe to pass through.
        business_name=payload.business_name,
    )
    db.add(user)
    db.flush()

    if user.role == UserRole.TRADER:
        db.add(TraderSettings(user_id=user.id, trading_enabled=True))
    else:
        db.add(
            SubscriberSettings(
                user_id=user.id,
                copy_enabled=False,
                multiplier=Decimal("1.000"),
            )
        )

    audit.record(
        db,
        actor_user_id=user.id,
        action="user.register",
        entity_type="user",
        entity_id=user.id,
        metadata={"role": user.role.value},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    ip = client_ip(request)
    # Brute-force gate: reject before touching the password hash if this
    # account is locked (too many recent failures) or the source IP has
    # blown its attempt budget. Returns 429 with Retry-After.
    if rate_limit.login_locked(payload.email) or rate_limit.login_ip_throttled(ip):
        audit.record(
            db,
            actor_user_id=None,
            action="user.login_rate_limited",
            metadata={"email": payload.email},
            ip_address=ip,
        )
        db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too_many_attempts",
            headers={"Retry-After": str(rate_limit.RETRY_AFTER_S)},
        )

    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        rate_limit.record_login_failure(payload.email)
        audit.record(
            db,
            actor_user_id=user.id if user else None,
            action="user.login_failed",
            metadata={"email": payload.email},
            ip_address=ip,
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="user_inactive")

    # Successful auth — clear the failed-attempt counter for this account.
    rate_limit.reset_login_failures(payload.email)
    audit.record(
        db,
        actor_user_id=user.id,
        action="user.login",
        ip_address=ip,
    )
    db.commit()
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshIn, db: Session = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong_token_type")
    import uuid as _uuid

    user = db.get(User, _uuid.UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user_inactive")
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user
