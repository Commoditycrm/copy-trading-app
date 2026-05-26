"""Admin-only API endpoints.

All routes require the ADMIN role (enforced by require_admin dependency).
These are internal platform-operator tools — never expose to traders or
subscribers.

Routes
------
GET  /api/admin/stats                  Dashboard stats (user counts, trades today)
GET  /api/admin/users                  List all users
PATCH /api/admin/users/{id}/activate   Set user.is_active = True
PATCH /api/admin/users/{id}/deactivate Set user.is_active = False
PATCH /api/admin/users/{id}/role       Change user role

GET  /api/admin/load-test/count        Count seeded fake subscribers
POST /api/admin/load-test/seed         Seed N fake subscribers for a trader
POST /api/admin/load-test/cleanup      Delete all fake-load-test-* users
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.services.crypto import encrypt_json

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ─── Constants matching seed_fake_subscribers.py ──────────────────────────────
_EMAIL_PREFIX = "fake-load-test-"
_EMAIL_DOMAIN = "@example.invalid"


def _fake_email(index: int) -> str:
    return f"{_EMAIL_PREFIX}{index:04d}{_EMAIL_DOMAIN}"


# ─── Schemas ──────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: str
    email: str
    role: str
    display_name: Optional[str]
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class RoleChangeIn(BaseModel):
    role: str = Field(pattern="^(trader|subscriber|admin)$")


class SeedIn(BaseModel):
    trader_email: str
    count: int = Field(default=50, ge=1, le=500)
    multiplier: float = Field(default=1.0, ge=0.01, le=10.0)


class CleanupIn(BaseModel):
    trader_email: Optional[str] = None


# ─── Stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    total_users  = db.execute(select(func.count(User.id))).scalar_one()
    traders      = db.execute(select(func.count(User.id)).where(User.role == UserRole.TRADER)).scalar_one()
    subscribers  = db.execute(select(func.count(User.id)).where(User.role == UserRole.SUBSCRIBER)).scalar_one()
    admins       = db.execute(select(func.count(User.id)).where(User.role == UserRole.ADMIN)).scalar_one()
    active_users = db.execute(select(func.count(User.id)).where(User.is_active.is_(True))).scalar_one()

    today_start  = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = db.execute(
        select(func.count(Order.id)).where(Order.created_at >= today_start)
    ).scalar_one()

    # Fake load-test subscriber count
    fake_subs = db.execute(
        select(func.count(User.id)).where(
            User.email.like(f"{_EMAIL_PREFIX}%{_EMAIL_DOMAIN}")
        )
    ).scalar_one()

    return {
        "total_users":   total_users,
        "traders":       traders,
        "subscribers":   subscribers,
        "admins":        admins,
        "active_users":  active_users,
        "trades_today":  trades_today,
        "fake_test_subs": fake_subs,
    }


# ─── User management ──────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[User]:
    return list(
        db.execute(select(User).order_by(User.created_at.desc())).scalars()
    )


@router.patch("/users/{user_id}/activate")
def activate_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user_not_found")
    user.is_active = True
    db.commit()
    log.info("admin activated user %s", user.email)
    return {"ok": True, "user_id": str(user_id), "is_active": True}


@router.patch("/users/{user_id}/deactivate")
def deactivate_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user_not_found")
    if user.role == UserRole.ADMIN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="cannot_deactivate_admin")
    user.is_active = False
    db.commit()
    log.info("admin deactivated user %s", user.email)
    return {"ok": True, "user_id": str(user_id), "is_active": False}


@router.patch("/users/{user_id}/role")
def change_role(
    user_id: uuid.UUID,
    payload: RoleChangeIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user_not_found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="cannot_change_own_role")
    user.role = UserRole(payload.role)
    db.commit()
    log.info("admin changed role of %s to %s", user.email, payload.role)
    return {"ok": True, "user_id": str(user_id), "role": payload.role}


# ─── Load-test subscriber management ─────────────────────────────────────────

@router.get("/load-test/count")
def load_test_count(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Return counts of seeded fake-load-test users, broker accounts, and active following."""
    users = list(
        db.execute(
            select(User).where(User.email.like(f"{_EMAIL_PREFIX}%{_EMAIL_DOMAIN}"))
        ).scalars()
    )
    user_ids = [u.id for u in users]

    accounts = 0
    following = 0
    if user_ids:
        accounts = db.execute(
            select(func.count(BrokerAccount.id)).where(
                BrokerAccount.user_id.in_(user_ids),
                BrokerAccount.broker == BrokerName.FAKE,
            )
        ).scalar_one()
        following = db.execute(
            select(func.count(SubscriberSettings.user_id)).where(
                SubscriberSettings.user_id.in_(user_ids),
                SubscriberSettings.copy_enabled.is_(True),
                SubscriberSettings.following_trader_id.isnot(None),
            )
        ).scalar_one()

    return {
        "seeded_users":       len(users),
        "fake_broker_accounts": accounts,
        "actively_following": following,
    }


@router.post("/load-test/seed", status_code=status.HTTP_201_CREATED)
def load_test_seed(
    payload: SeedIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Create up to `count` fake subscribers following the specified trader.
    Idempotent — re-running skips already-seeded users."""
    from passlib.hash import bcrypt as _bcrypt  # noqa: PLC0415

    trader = db.execute(
        select(User).where(User.email == payload.trader_email)
    ).scalar_one_or_none()
    if trader is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="trader_not_found")
    if trader.role != UserRole.TRADER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"user is {trader.role.value}, expected trader",
        )

    empty_creds = encrypt_json({})
    shared_pw   = _bcrypt.hash("fake-load-test-not-for-login")

    existing = set(
        db.execute(
            select(User.email).where(
                User.email.like(f"{_EMAIL_PREFIX}%{_EMAIL_DOMAIN}")
            )
        ).scalars()
    )

    multiplier = Decimal(str(payload.multiplier))
    created = 0
    for i in range(payload.count):
        email = _fake_email(i)
        if email in existing:
            continue
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=shared_pw,
            role=UserRole.SUBSCRIBER,
            display_name=f"Load Test {i:04d}",
            is_active=True,
        )
        db.add(user)
        db.flush()

        db.add(SubscriberSettings(
            user_id=user.id,
            following_trader_id=trader.id,
            copy_enabled=True,
            multiplier=multiplier,
        ))
        db.add(BrokerAccount(
            id=uuid.uuid4(),
            user_id=user.id,
            broker=BrokerName.FAKE,
            label=f"Fake Broker {i:04d}",
            is_paper=True,
            supports_fractional=True,
            encrypted_credentials=empty_creds,
            connection_status="connected",
            broker_account_number=f"FAKE-{i:04d}",
        ))
        created += 1

    db.commit()
    log.info("load-test seed: created %d new fake subscribers for trader %s",
             created, payload.trader_email)

    # Bust Redis subscriber cache so fanout picks up new rows immediately.
    try:
        from app.services import cache as cache_svc  # noqa: PLC0415
        cache_svc.invalidate_subscribers_for_trader(trader.id)
    except Exception:  # noqa: BLE001
        log.warning("could not invalidate subscriber cache after seed")

    return {
        "created":  created,
        "skipped":  payload.count - created,
        "total":    payload.count,
    }


@router.post("/load-test/cleanup")
def load_test_cleanup(
    payload: CleanupIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Delete every fake-load-test-* user. CASCADE drops their broker
    accounts, subscriber settings, orders, and notifications."""
    before_ids = list(
        db.execute(
            select(User.id).where(
                User.email.like(f"{_EMAIL_PREFIX}%{_EMAIL_DOMAIN}")
            )
        ).scalars()
    )
    if not before_ids:
        return {"deleted": 0}

    db.execute(
        delete(User).where(
            User.email.like(f"{_EMAIL_PREFIX}%{_EMAIL_DOMAIN}")
        )
    )
    db.commit()
    log.info("load-test cleanup: deleted %d fake users", len(before_ids))

    # Invalidate trader's subscriber cache if requested.
    if payload.trader_email:
        try:
            trader = db.execute(
                select(User).where(User.email == payload.trader_email)
            ).scalar_one_or_none()
            if trader:
                from app.services import cache as cache_svc  # noqa: PLC0415
                cache_svc.invalidate_subscribers_for_trader(trader.id)
        except Exception:  # noqa: BLE001
            log.warning("could not invalidate cache after cleanup")

    return {"deleted": len(before_ids)}
