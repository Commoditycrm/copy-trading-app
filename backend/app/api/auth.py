import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.config import get_settings
from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    decode_email_verification_token,
    decode_password_reset_token,
    decode_token,
    hash_password,
    password_fingerprint_matches,
    verify_password,
)
from app.database import get_db
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.auth import (
    ForgotPasswordIn,
    LoginIn,
    MessageOut,
    RefreshIn,
    RegisterIn,
    ResendVerificationIn,
    ResetPasswordIn,
    TokenPair,
    UserOut,
    VerifyEmailIn,
)
from app.services import audit, rate_limit
from app.services.email import send_password_reset_email, send_verification_email


def _send_verification(background: BackgroundTasks, user: User) -> None:
    """Queue a verification email for ``user`` as a background task."""
    token = create_email_verification_token(str(user.id), user.email)
    link = f"{get_settings().frontend_base_url}/verify-email?token={token}"
    background.add_task(send_verification_email, user.email, link, user.display_name)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Returned by /forgot-password regardless of whether the email exists, so the
# endpoint can't be used to enumerate registered accounts.
_RESET_REQUESTED_MSG = (
    "If an account with that email exists, a password reset link has been sent."
)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> User:
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
    # Soft email verification: account is usable immediately, but we send a
    # confirmation link and the app nags with a banner until it's verified.
    _send_verification(background, user)
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


@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Start a password reset. Always returns the same message (no account
    enumeration). When the email maps to an active user, we mint a short-lived
    reset token bound to their current password hash and email a link to it.
    The email send runs as a background task so a slow/failing SendGrid call
    never blocks the response."""
    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()

    if user is not None and user.is_active:
        token = create_password_reset_token(str(user.id), user.password_hash)
        reset_link = f"{get_settings().frontend_base_url}/reset-password?token={token}"
        background.add_task(
            send_password_reset_email, user.email, reset_link, user.display_name
        )
        audit.record(
            db,
            actor_user_id=user.id,
            action="user.password_reset_requested",
            entity_type="user",
            entity_id=user.id,
            ip_address=client_ip(request),
        )
        db.commit()

    return MessageOut(detail=_RESET_REQUESTED_MSG)


@router.post("/reset-password", response_model=MessageOut)
def reset_password(
    payload: ResetPasswordIn,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Complete a password reset. Validates the token, confirms it was minted
    against the user's current password hash (single-use semantics), then sets
    the new password. A used or stale link fails the fingerprint check."""
    try:
        claims = decode_password_reset_token(payload.token)
        user = db.get(User, uuid.UUID(claims["sub"]))
    except (ValueError, KeyError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    if (
        user is None
        or not user.is_active
        or not password_fingerprint_matches(claims, user.password_hash)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    user.password_hash = hash_password(payload.new_password)
    audit.record(
        db,
        actor_user_id=user.id,
        action="user.password_reset",
        entity_type="user",
        entity_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return MessageOut(detail="Your password has been reset. You can now sign in.")


@router.post("/verify-email", response_model=MessageOut)
def verify_email(
    payload: VerifyEmailIn,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Confirm an email address from the link token. Idempotent — verifying an
    already-verified account succeeds. The token's ``eml`` claim must still
    match the user's current email (a link goes stale if the email changed)."""
    try:
        claims = decode_email_verification_token(payload.token)
        user = db.get(User, uuid.UUID(claims["sub"]))
    except (ValueError, KeyError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    if user is None or claims.get("eml") != user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_token")

    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = datetime.now(timezone.utc)
        audit.record(
            db,
            actor_user_id=user.id,
            action="user.email_verified",
            entity_type="user",
            entity_id=user.id,
            ip_address=client_ip(request),
        )
        db.commit()
    return MessageOut(detail="Your email has been verified.")


@router.post("/resend-verification", response_model=MessageOut)
def resend_verification(
    payload: ResendVerificationIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Re-send the verification email. Always returns the same message (no
    account enumeration); only actually sends for an existing, still-unverified
    user."""
    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()
    if user is not None and user.is_active and not user.email_verified:
        _send_verification(background, user)
        audit.record(
            db,
            actor_user_id=user.id,
            action="user.verification_resent",
            entity_type="user",
            entity_id=user.id,
            ip_address=client_ip(request),
        )
        db.commit()
    return MessageOut(
        detail="If your account needs verification, a new link has been sent."
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshIn, db: Session = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong_token_type")
    user = db.get(User, uuid.UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user_inactive")
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user
