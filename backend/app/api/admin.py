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

GET  /api/admin/performance/fanouts    All fanouts across all traders (admin view)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.dashboard_metrics import LoadTestRun, TestResult
from app.models.order import Order, OrderStatus
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.services import excel_export
from app.services.broker_names import heal_snaptrade_brokerage_names
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
    # Declare the real Python types so Pydantic v2 can serialize them to
    # JSON itself (UUID → "uuid-string", datetime → ISO 8601). Declaring
    # these as `str` made Pydantic strict-mode reject ORM values with a
    # "Input should be a valid string" ResponseValidationError — see
    # GET /api/admin/users hitting 500 on every fetch.
    id: uuid.UUID
    email: str
    role: str
    display_name: Optional[str]
    # Trader brand surfaced in the AppShell. Null for subscribers / admins.
    # The admin users page renders + edits this via PATCH .../business-name.
    business_name: Optional[str] = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleChangeIn(BaseModel):
    role: str = Field(pattern="^(trader|subscriber|admin)$")


class BusinessNameIn(BaseModel):
    """Trader brand / app name. 1–120 chars, whitespace stripped.

    Empty / whitespace-only is rejected — the field is mandatory for any
    trader (matches the RegisterIn validator). No null path: clearing
    isn't a supported admin action since the AppShell would silently
    revert to the "ARK" fallback for every follower, which is rarely
    what an admin actually wants."""

    business_name: str = Field(min_length=1, max_length=120)


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


@router.patch("/users/{user_id}/business-name")
def change_business_name(
    user_id: uuid.UUID,
    payload: BusinessNameIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Rename a trader's business / brand. Trader-only — for subscribers
    and admins business_name is meaningless (it's only ever surfaced as
    the AppShell wordmark, and the shell pulls it from the trader the
    subscriber follows). Bust the followed-by cache so every subscriber's
    next SubscriberSettings fetch sees the new brand without waiting for
    a 5-minute Redis TTL.
    """
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user_not_found")
    if user.role != UserRole.TRADER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="business_name only applies to traders",
        )
    new_name = payload.business_name.strip()
    if not new_name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="business_name_blank")
    old = user.business_name
    user.business_name = new_name
    db.commit()
    log.info("admin set business_name for %s: %r -> %r", user.email, old, new_name)
    # Subscribers see the followed trader's business_name via SubscriberSettings;
    # the row is cached per-trader in Redis. Invalidate so the rename is
    # reflected on the next fetch. Best-effort — a cache miss is harmless.
    try:
        from app.services import cache as cache_svc  # noqa: PLC0415
        cache_svc.invalidate_subscribers_for_trader(user.id)
    except Exception:  # noqa: BLE001
        log.warning("could not invalidate subscriber cache after rename")
    return {"ok": True, "user_id": str(user_id), "business_name": new_name}


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


# ─── Performance (all traders) ────────────────────────────────────────────────

# Cap on how many parent fanouts we load to compute window aggregates, so a wide
# range (e.g. 30d × all traders) can't pull an unbounded set. If the window
# exceeds this we aggregate over the most-recent N and flag it (metrics.truncated).
_AGG_PARENT_CAP = 2000


def _median(values: list[int | None]) -> int | None:
    """Median of a list of ints, skipping Nones (None if empty)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else int((vals[mid - 1] + vals[mid]) / 2)


def _broker_key(account: "BrokerAccount | None") -> tuple[str, str]:
    """(grouping key, display label) for a subscriber's broker account.

    SnapTrade routes are grouped by their *underlying* brokerage so "Webull via
    SnapTrade" is distinct from "Robinhood via SnapTrade"; direct integrations
    group by the broker enum value.
    """
    if account is None:
        return ("unknown", "Unknown")
    if account.broker == BrokerName.SNAPTRADE and account.brokerage_name:
        return (f"st:{account.brokerage_name.lower()}", f"{account.brokerage_name} (ST)")
    val = account.broker.value if account.broker else None
    return (val or "unknown", val or (account.label or "Unknown"))


def _fanout_window_query(trader_id, from_, to):
    """Base select for parent fanouts in a (trader, time) window.

    Window anchors on COALESCE(trader_submitted_at, created_at) so externally
    placed orders (which carry trader_submitted_at) and in-app ones both filter
    correctly.
    """
    from app.api.performance import realtime_fanout_clause  # noqa: PLC0415

    anchor = func.coalesce(Order.trader_submitted_at, Order.created_at)
    q = select(Order).where(
        Order.parent_order_id.is_(None),
        Order.fanned_out_to_subscribers.is_(True),
        realtime_fanout_clause(),
    )
    if trader_id is not None:
        q = q.where(Order.user_id == trader_id)
    if from_ is not None:
        q = q.where(anchor >= from_)
    if to is not None:
        q = q.where(anchor <= to)
    return q


@router.get("/performance/fanouts")
def admin_list_fanouts(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    trader_id: uuid.UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    broker: str | None = None,
    limit: int = 50,
) -> dict:
    """All fanouts across every trader — newest first, filterable.

    Query params:
      - trader_id : restrict to one trader's orders.
      - from / to : window on COALESCE(trader_submitted_at, created_at) (UTC ISO).
      - broker    : restrict child mirrors to one broker enum value (e.g.
                    "alpaca") for the latency panels.
      - limit     : how many fanouts to return in the table. The `metrics`
                    aggregate over the WHOLE window (capped), not just this page.
    """
    from app.api.performance import (  # noqa: PLC0415
        _SUCCESS_STATUSES,
        _ms_between,
        _serialize_fanout,
    )

    _empty_metrics = {
        "fanouts_shown": 0, "trade_count": 0, "avg_fanout_ms": None,
        "max_fanout_ms": None, "avg_total_ms": None, "median_platform_ms": None,
        "median_broker_ms": None, "success_rate": None, "pct_within_1s": None,
        "active_subscribers": 0, "truncated": False,
    }

    base = _fanout_window_query(trader_id, from_, to)
    parents = list(db.execute(
        base.order_by(Order.created_at.desc()).limit(_AGG_PARENT_CAP + 1)
    ).scalars())
    truncated = len(parents) > _AGG_PARENT_CAP
    parents = parents[:_AGG_PARENT_CAP]
    if not parents:
        return {"fanouts": [], "metrics": _empty_metrics}

    parent_by_id = {p.id: p for p in parents}
    parent_ids = list(parent_by_id)

    children = list(db.execute(
        select(Order).where(Order.parent_order_id.in_(parent_ids))
    ).scalars())

    # Broker accounts for child broker resolution / filtering.
    acct_ids = {c.broker_account_id for c in children if c.broker_account_id is not None}
    accounts: dict[uuid.UUID, BrokerAccount] = {}
    if acct_ids:
        accounts = {a.id: a for a in db.execute(
            select(BrokerAccount).where(BrokerAccount.id.in_(acct_ids))
        ).scalars()}

    # Resolve NULL SnapTrade brokerage names the same way the trader endpoint
    # does — without this the admin table shows "snaptrade" where the trader's
    # own Performance page shows the real broker ("Webull (ST)").
    heal_snaptrade_brokerage_names(db, accounts.values())

    if broker:
        children = [
            c for c in children
            if (a := accounts.get(c.broker_account_id)) is not None
            and a.broker is not None and a.broker.value == broker
        ]

    children_by_parent: dict[uuid.UUID, list[Order]] = {pid: [] for pid in parent_ids}
    for c in children:
        if c.parent_order_id in children_by_parent:
            children_by_parent[c.parent_order_id].append(c)

    sub_ids = {c.user_id for c in children}
    subscribers = {u.id: u for u in db.execute(
        select(User).where(User.id.in_(sub_ids))
    ).scalars()} if sub_ids else {}
    trader_ids = {p.user_id for p in parents}
    traders = {u.id: u for u in db.execute(
        select(User).where(User.id.in_(trader_ids))
    ).scalars()}

    # ── Window aggregates (over ALL children in window, not just the shown page) ─
    child_total = len(children)
    success = sum(1 for c in children if c.status in _SUCCESS_STATUSES)
    submitted_children = sum(1 for c in children if c.submitted_at is not None)
    within_1s = sum(
        1 for c in children
        if (lag := _ms_between(parent_by_id[c.parent_order_id].created_at,
                               c.submitted_at)) is not None and lag <= 1000
    )
    broker_ms = [c.broker_call_ms for c in children if c.broker_call_ms is not None]
    platform_ms: list[int] = []
    for pid, kids in children_by_parent.items():
        last = max((c.submitted_at for c in kids if c.submitted_at), default=None)
        d = _ms_between(parent_by_id[pid].created_at, last)
        if d is not None:
            platform_ms.append(d)

    # ── Serialize the most-recent `limit` for the table ─────────────────────────
    fanouts = []
    for p in parents[:limit]:
        s = _serialize_fanout(p, children_by_parent.get(p.id, []), subscribers, accounts)
        t = traders.get(p.user_id)
        s["trader_email"] = t.email if t else None
        s["trader_display_name"] = t.display_name if t else None
        fanouts.append(s)

    # Client-facing total latency (trader submit → last subscriber's broker
    # accepted), averaged over the shown fanouts — parity with the trader
    # Performance view's "Total Latency" card, which the admin per-trader page
    # reuses.
    totals = [s["total_ms"] for s in fanouts if s.get("total_ms") is not None]

    metrics = {
        "fanouts_shown": len(fanouts),
        "trade_count": len(parents),
        "avg_fanout_ms": int(sum(platform_ms) / len(platform_ms)) if platform_ms else None,
        "max_fanout_ms": max(platform_ms) if platform_ms else None,
        "avg_total_ms": int(sum(totals) / len(totals)) if totals else None,
        "median_platform_ms": _median(platform_ms),
        "median_broker_ms": _median(broker_ms),
        "success_rate": round(success / child_total, 4) if child_total else None,
        "pct_within_1s": round(within_1s / submitted_children, 4) if submitted_children else None,
        "active_subscribers": len(sub_ids),
        "truncated": truncated,
    }
    return {"fanouts": fanouts, "metrics": metrics}


def _num(v):
    """Serialized payloads carry Decimals as strings so JSON stays exact.
    Excel needs real numbers — a string here is a cell you can't sum."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return str(v)


def _ts(v):
    """ISO string -> datetime, so Excel sorts it as a date not as text."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return str(v)


def _fanout_export_columns() -> list[excel_export.Column]:
    """One row per MIRROR, with the parent trade's context repeated.

    Flattened rather than nested because that's what Excel can actually work
    with — filter by subscriber, group by broker, average the lags. Keys match
    _serialize_fanout / _serialize_child exactly (see api/performance.py).
    """
    C = excel_export.Column
    M, D, I = "#,##0.00######", "yyyy-mm-dd hh:mm:ss", "#,##0"
    p = lambda k: (lambda r: r[0].get(k))          # noqa: E731 — parent field
    c = lambda k: (lambda r: (r[1] or {}).get(k))  # noqa: E731 — mirror field
    return [
        # ── the trade ──────────────────────────────────────────────
        # Keys are _serialize_fanout's, plus trader_email/trader_display_name
        # which admin_list_fanouts injects afterwards. Note the parent carries
        # no status of its own — the fanout's health is subscribers.errors.
        C("Trade Time (UTC)", lambda r: _ts(r[0].get("trader_submitted_at") or r[0].get("detected_at")), 19, D),
        C("Trader", p("trader_display_name"), 20),
        C("Trader Email", p("trader_email"), 26),
        C("Symbol", p("symbol"), 12),
        C("Side", lambda r: (r[0].get("side") or "").upper(), 8),
        C("Type", p("instrument_type"), 9),
        C("Trade Qty", lambda r: _num(r[0].get("quantity")), 11, M),
        C("Expected Price", lambda r: _num(r[0].get("expected_price")), 13, M),
        C("Filled Price", lambda r: _num(r[0].get("filled_avg_price")), 13, M),
        C("Subscribers", lambda r: (r[0].get("subscribers") or {}).get("total"), 11, I),
        C("Mirrors Submitted", lambda r: (r[0].get("subscribers") or {}).get("submitted"), 15, I),
        C("Mirror Errors", lambda r: (r[0].get("subscribers") or {}).get("errors"), 12, I),
        C("Detection Lag (ms)", lambda r: r[0].get("detection_lag_ms"), 15, I),
        C("Fanout Duration (ms)", lambda r: r[0].get("fanout_duration_ms"), 17, I),
        C("Total Time (ms)", lambda r: r[0].get("total_ms"), 14, I),
        # ── the mirror ─────────────────────────────────────────────
        C("Subscriber", c("subscriber_name"), 20),
        C("Subscriber Email", c("subscriber_email"), 26),
        C("Mirror Status", c("status"), 14),
        C("Broker", c("broker_name"), 14),
        C("Mirror Qty", lambda r: _num((r[1] or {}).get("quantity")), 11, M),
        C("Mirror Filled Qty", lambda r: _num((r[1] or {}).get("filled_quantity")), 14, M),
        C("Mirror Expected Price", lambda r: _num((r[1] or {}).get("expected_price")), 17, M),
        C("Mirror Filled Price", lambda r: _num((r[1] or {}).get("filled_avg_price")), 16, M),
        C("Pick Lag (ms)", c("pick_lag_ms"), 12, I),
        C("Eligibility Lag (ms)", c("eligibility_lag_ms"), 16, I),
        C("Broker Lag (ms)", c("broker_lag_ms"), 14, I),
        C("Subscriber Lag (ms)", c("subscriber_lag_ms"), 16, I),
        C("Reject Reason", c("reject_reason"), 40),
        C("Mirror Broker Order ID", c("broker_order_id"), 26),
        C("Parent Order ID", p("parent_order_id"), 36),
    ]


@router.get("/performance/export")
def admin_export_fanouts(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    trader_id: uuid.UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    broker: str | None = None,
    search: str | None = Query(default=None, description="Symbol / trader, as the admin table's box"),
    side: str | None = Query(default=None, description="all | buy | sell"),
) -> Response:
    """Fanout data as .xlsx, one row per subscriber mirror.

    Builds on admin_list_fanouts rather than re-querying, so the file is
    generated from the exact payload the table renders — the export can't drift
    away from the UI the way a parallel query would.
    """
    payload = admin_list_fanouts(
        db=db, _=admin, trader_id=trader_id, from_=from_, to=to,
        broker=broker, limit=_AGG_PARENT_CAP,
    )
    fanouts = payload.get("fanouts") or []

    # The admin table applies these two IN THE BROWSER, over this same
    # serialized payload — so filter the payload rather than the query. Same
    # data, same predicate, so the file can't disagree with the screen.
    # Mirrors `visibleFanouts` in app/admin/performance/page.tsx.
    needle = (search or "").strip().lower()
    if needle:
        fanouts = [
            f for f in fanouts
            if needle in (f.get("symbol") or "").lower()
            or needle in (f.get("trader_email") or "").lower()
            or needle in (f.get("trader_display_name") or "").lower()
        ]
    if side and side != "all":
        fanouts = [f for f in fanouts if (f.get("side") or "") == side]

    # Flatten. A trade that reached NOBODY still gets a row (blank mirror
    # columns) — a fanout with zero subscribers is exactly what an admin is
    # usually hunting for, so it must not vanish from the sheet.
    rows: list[tuple[dict, dict | None]] = []
    for f in fanouts:
        children = f.get("children") or []
        if not children:
            rows.append((f, None))
        else:
            rows.extend((f, c) for c in children)

    now = datetime.now(timezone.utc)
    data = excel_export.build_workbook(
        columns=_fanout_export_columns(),
        rows=rows,
        sheet_title="Fanouts",
        meta=(
            ("Exported (UTC)", now.replace(tzinfo=None)),
            ("Exported by", admin.email),
            ("Trader filter", str(trader_id) if trader_id else "(all traders)"),
            ("Broker filter", broker or "(all brokers)"),
            ("Search", search or "(none)"),
            ("Side filter", side or "all"),
            ("From", from_.replace(tzinfo=None) if from_ else "(all time)"),
            ("To", to.replace(tzinfo=None) if to else "(all time)"),
            ("Trades", len(fanouts)),
            ("Mirror rows", len(rows)),
            # admin_list_fanouts caps at _AGG_PARENT_CAP parents. Say so in the
            # file — a silently truncated export reads as the whole picture.
            ("Truncated", "YES — capped at %d trades" % _AGG_PARENT_CAP
             if (payload.get("metrics") or {}).get("truncated") else "no"),
        ),
    )
    return Response(
        content=data,
        media_type=excel_export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition":
                f'attachment; filename="{excel_export.filename("fanouts", when=now)}"',
        },
    )


@router.get("/rejected-orders")
def admin_rejected_orders(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    role: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = 100,
) -> dict:
    """Every failed order across the platform — status REJECTED or
    RETRY_PENDING — newest first, with the broker's reason. Lets an admin see
    at a glance why a trader's or subscriber's trade didn't go through.

    Query params:
      - role      : "trader" | "subscriber" to restrict to one side.
      - from / to : window on Order.created_at (UTC ISO).
      - limit     : cap on rows returned (1–500).
    """
    limit = max(1, min(limit, 500))
    q = (
        select(Order, User)
        .join(User, User.id == Order.user_id)
        .where(Order.status.in_([OrderStatus.REJECTED, OrderStatus.RETRY_PENDING]))
    )
    if role:
        try:
            q = q.where(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(422, f"invalid role: {role!r}")
    if from_ is not None:
        q = q.where(Order.created_at >= from_)
    if to is not None:
        q = q.where(Order.created_at <= to)
    q = q.order_by(Order.created_at.desc()).limit(limit + 1)

    rows = list(db.execute(q).all())
    truncated = len(rows) > limit
    rows = rows[:limit]

    # Resolve broker names in bulk for the display column.
    acct_ids = {o.broker_account_id for o, _u in rows if o.broker_account_id is not None}
    accounts: dict[uuid.UUID, BrokerAccount] = {}
    if acct_ids:
        accounts = {a.id: a for a in db.execute(
            select(BrokerAccount).where(BrokerAccount.id.in_(acct_ids))
        ).scalars()}

    rejections = []
    for o, u in rows:
        acct = accounts.get(o.broker_account_id) if o.broker_account_id else None
        rejections.append({
            "order_id": str(o.id),
            "user_id": str(o.user_id),
            "user_email": u.email if u else None,
            "user_name": u.display_name if u else None,
            "user_role": u.role.value if u else None,
            # parent_order_id set → this is a subscriber's mirror of a trader's
            # order; null → the user's own (trader or standalone) order.
            "is_mirror": o.parent_order_id is not None,
            "symbol": o.symbol,
            "side": o.side.value,
            "instrument_type": o.instrument_type.value,
            "quantity": str(o.quantity),
            "status": o.status.value,
            "reject_reason": o.reject_reason,
            "broker": acct.broker.value if acct and acct.broker else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            # Payload fields — let the admin panel reconstruct the order the way
            # it was sent to the broker. The raw broker request body isn't
            # persisted, so these columns are the source of truth.
            "order_type": o.order_type.value,
            "limit_price": str(o.limit_price) if o.limit_price is not None else None,
            "stop_price": str(o.stop_price) if o.stop_price is not None else None,
            "option_expiry": o.option_expiry.isoformat() if o.option_expiry else None,
            "option_strike": str(o.option_strike) if o.option_strike is not None else None,
            "option_right": o.option_right.value if o.option_right else None,
            "is_closing": o.is_closing,
            "broker_order_id": o.broker_order_id,
            # null broker_call_ms + null broker_order_id ⇒ the order never
            # reached the broker (rejected internally, e.g. credential decrypt).
            "broker_call_ms": o.broker_call_ms,
        })
    return {"rejections": rejections, "truncated": truncated}


@router.get("/performance/by-broker")
def admin_performance_by_broker(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    trader_id: uuid.UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
) -> list[dict]:
    """Per-broker leaderboard over the window.

    Groups child mirror orders by the subscriber's broker (SnapTrade split by
    underlying brokerage) and reports counts, success rate, and median latencies.
    Sorted by median end-to-end (subscriber) lag — fastest broker first.
    """
    from app.api.performance import _SUCCESS_STATUSES, _ms_between  # noqa: PLC0415

    parents = list(db.execute(
        _fanout_window_query(trader_id, from_, to)
        .order_by(Order.created_at.desc()).limit(_AGG_PARENT_CAP)
    ).scalars())
    if not parents:
        return []
    parent_by_id = {p.id: p for p in parents}

    children = list(db.execute(
        select(Order).where(Order.parent_order_id.in_(list(parent_by_id)))
    ).scalars())
    acct_ids = {c.broker_account_id for c in children if c.broker_account_id is not None}
    accounts = {a.id: a for a in db.execute(
        select(BrokerAccount).where(BrokerAccount.id.in_(acct_ids))
    ).scalars()} if acct_ids else {}

    groups: dict[str, dict] = {}
    for c in children:
        key, label = _broker_key(accounts.get(c.broker_account_id))
        g = groups.setdefault(key, {
            "broker": label, "accounts": set(), "mirrors": 0, "success": 0,
            "detection": [], "broker_ms": [], "subscriber_ms": [],
        })
        g["mirrors"] += 1
        if c.broker_account_id is not None:
            g["accounts"].add(c.broker_account_id)
        if c.status in _SUCCESS_STATUSES:
            g["success"] += 1
        if c.broker_call_ms is not None:
            g["broker_ms"].append(c.broker_call_ms)
        parent = parent_by_id.get(c.parent_order_id)
        if parent is not None:
            g["detection"].append(_ms_between(parent.submitted_at, parent.created_at))
            g["subscriber_ms"].append(_ms_between(parent.created_at, c.submitted_at))

    out = [{
        "broker": g["broker"],
        "accounts": len(g["accounts"]),
        "mirrors": g["mirrors"],
        "success_rate": round(g["success"] / g["mirrors"], 4) if g["mirrors"] else None,
        "median_detection_ms": _median(g["detection"]),
        "median_broker_ms": _median(g["broker_ms"]),
        "median_subscriber_lag_ms": _median(g["subscriber_ms"]),
    } for g in groups.values()]
    out.sort(key=lambda r: (r["median_subscriber_lag_ms"] is None, r["median_subscriber_lag_ms"] or 0))
    return out


# ─── Testing results + load-test history ──────────────────────────────────────
#
# Two append-only logs (see models/dashboard_metrics.py). The GETs feed the
# dashboard's Testing and Load-test panels; the POSTs let the test runner / CI
# (or a load-test trigger) record a row.


class TestResultIn(BaseModel):
    suite: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: int | None = None
    source: str | None = None
    commit_sha: str | None = None


class LoadTestRunIn(BaseModel):
    subscribers: int
    total_ms: int | None = None
    waves: int | None = None
    errors: int = 0
    note: str | None = None


def _test_out(r: TestResult) -> dict:
    total = r.passed + r.failed
    return {
        "suite": r.suite,
        "passed": r.passed,
        "failed": r.failed,
        "skipped": r.skipped,
        "duration_ms": r.duration_ms,
        "source": r.source,
        "commit_sha": r.commit_sha,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "pass_rate": round(r.passed / total, 4) if total else None,
    }


def _loadrun_out(r: LoadTestRun) -> dict:
    return {
        "id": str(r.id),
        "subscribers": r.subscribers,
        "total_ms": r.total_ms,
        "waves": r.waves,
        "errors": r.errors,
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/tests/latest")
def admin_tests_latest(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict]:
    """Latest result row per test suite (newest scan first, capped)."""
    rows = db.execute(
        select(TestResult).order_by(TestResult.created_at.desc()).limit(500)
    ).scalars()
    latest: dict[str, TestResult] = {}
    for r in rows:
        latest.setdefault(r.suite, r)  # first seen = newest (desc order)
    return [_test_out(r) for r in sorted(latest.values(), key=lambda r: r.suite)]


@router.post("/tests/results", status_code=status.HTTP_201_CREATED)
def admin_record_test_result(
    payload: TestResultIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    r = TestResult(**payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return _test_out(r)


@router.get("/load-test/history")
def admin_load_test_history(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    limit: int = 20,
) -> list[dict]:
    """Recent load-test runs, newest first."""
    rows = db.execute(
        select(LoadTestRun).order_by(LoadTestRun.created_at.desc()).limit(limit)
    ).scalars()
    return [_loadrun_out(r) for r in rows]


@router.post("/load-test/runs", status_code=status.HTTP_201_CREATED)
def admin_record_load_test_run(
    payload: LoadTestRunIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    r = LoadTestRun(**payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return _loadrun_out(r)


# ─── Broker-connection health ─────────────────────────────────────────────────
#
# DB-backed (broker_accounts), so it's readable from the web tier. Surfaces
# accounts whose mirrors WON'T fire — disconnected / errored connections,
# auto-pull turned off, or a stale last-sync.


@router.get("/broker-health")
def admin_broker_health(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    only_problems: bool = False,
) -> dict:
    """Every connected broker account with its connection health. An account is
    'healthy' only if connection_status == 'connected' AND auto_pull_orders is
    on — otherwise its owner's mirrors won't fire."""
    rows = list(db.execute(select(BrokerAccount)).scalars())
    user_ids = {a.user_id for a in rows}
    users = {u.id: u for u in db.execute(
        select(User).where(User.id.in_(user_ids))
    ).scalars()} if user_ids else {}

    def _label(a: BrokerAccount) -> str:
        if a.broker == BrokerName.SNAPTRADE and a.brokerage_name:
            return f"{a.brokerage_name} (ST)"
        return (a.broker.value if a.broker else None) or a.label

    accounts = []
    for a in rows:
        u = users.get(a.user_id)
        healthy = a.connection_status == "connected" and a.auto_pull_orders
        accounts.append({
            "user_email": u.email if u else None,
            "user_name": u.display_name if u else None,
            "broker": _label(a),
            "is_paper": a.is_paper,
            "connection_status": a.connection_status,
            "last_error": a.last_error,
            "auto_pull_orders": a.auto_pull_orders,
            "last_activity_sync_at": a.last_activity_sync_at.isoformat() if a.last_activity_sync_at else None,
            "balance_updated_at": a.balance_updated_at.isoformat() if a.balance_updated_at else None,
            "healthy": healthy,
        })
    if only_problems:
        accounts = [x for x in accounts if not x["healthy"]]
    # Problems first, then by broker.
    accounts.sort(key=lambda x: (x["healthy"], x["broker"] or ""))

    summary = {
        "total": len(rows),
        "connected": sum(1 for a in rows if a.connection_status == "connected"),
        "problems": sum(1 for a in rows if a.connection_status != "connected"),
        "auto_pull_off": sum(1 for a in rows if not a.auto_pull_orders),
    }
    return {"summary": summary, "accounts": accounts}


# ─── Listener health ──────────────────────────────────────────────────────────
#
# Listener state lives in the worker process (in-memory) and is mirrored to
# Redis by listener_state, so the web tier can read every trader's current
# detection-listener state here. A down listener = that trader's trades aren't
# detected and nothing mirrors.


@router.get("/listener-health")
def admin_listener_health(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    from app.services import listener_state  # noqa: PLC0415

    statuses = listener_state.get_all_statuses()  # {trader_id_str: status_dict}
    trader_ids = []
    for t in statuses:
        try:
            trader_ids.append(uuid.UUID(t))
        except ValueError:
            continue
    traders = {str(u.id): u for u in db.execute(
        select(User).where(User.id.in_(trader_ids))
    ).scalars()} if trader_ids else {}

    listeners = []
    for tid, st in statuses.items():
        u = traders.get(tid)
        listeners.append({
            "trader_id": tid,
            "trader_email": u.email if u else None,
            "trader_name": u.display_name if u else None,
            "state": st.get("state"),
            "last_event_at": st.get("last_event_at"),
            "state_changed_at": st.get("state_changed_at"),
            "last_error": st.get("last_error"),
        })
    # Down/degraded first, then by trader.
    listeners.sort(key=lambda x: (x["state"] == "connected", x["trader_email"] or ""))

    summary = {
        "total": len(listeners),
        "connected": sum(1 for x in listeners if x["state"] == "connected"),
        "down": sum(1 for x in listeners if x["state"] != "connected"),
    }
    return {"summary": summary, "listeners": listeners}


# ── Platform-config: fanout batch threshold ─────────────────────────────────
#
# Runtime-tunable knob that copy_engine reads on every fanout to decide
# between the per-iteration (small N, low floor) and batched (large N, flat
# scaling) code paths. Env default lives in Settings.fanout_batch_threshold;
# this endpoint pair sets / clears a Redis override on top of that.


class FanoutThresholdOut(BaseModel):
    """Effective + default + override values for the admin UI to render."""

    default: int
    override: int | None
    effective: int


class FanoutThresholdIn(BaseModel):
    """Pass ``threshold=null`` to reset to the env default. Bounded so a
    misclick can't disable the hybrid entirely (a threshold of 0 would
    always batch — fine, but extreme; >10000 would never batch — also fine
    but extreme). 1–10000 covers every realistic deployment."""

    threshold: int | None = Field(default=None, ge=1, le=10000)


@router.get("/config/fanout-batch-threshold", response_model=FanoutThresholdOut)
def get_fanout_threshold(
    _: User = Depends(require_admin),
) -> dict:
    from app.services.platform_config import get_fanout_batch_threshold_state
    return get_fanout_batch_threshold_state()


@router.patch("/config/fanout-batch-threshold", response_model=FanoutThresholdOut)
def set_fanout_threshold(
    payload: FanoutThresholdIn,
    _: User = Depends(require_admin),
) -> dict:
    from app.services.platform_config import (
        get_fanout_batch_threshold_state,
        set_fanout_batch_threshold,
    )
    try:
        set_fanout_batch_threshold(payload.threshold)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return get_fanout_batch_threshold_state()


# ── Platform-config: Alpaca P&L poll interval ──────────────────────────
#
# pnl_poller hits Alpaca's GET /v2/account once per connected account
# per tick. The interval is runtime-tunable so an admin can throttle
# down to save the 200/min/account budget on a heavy-fanout day, or
# crank up to lower kill-switch latency. Stored as a Redis override
# on top of the env default (Settings.alpaca_pnl_poll_interval_s).


class AlpacaPollIntervalOut(BaseModel):
    """Effective + default + override + bound values for the admin UI."""

    default: int
    override: int | None
    effective: int
    # Min/max the setter accepts. Surfaced so the UI input can bound
    # itself client-side and the user never sends a value that comes
    # back 422.
    min: int
    max: int


class AlpacaPollIntervalIn(BaseModel):
    """Pass ``interval_s=null`` to reset to the env default. Range
    bounded by the same min/max returned in the GET payload — see the
    setter for the underlying rationale."""

    interval_s: int | None = Field(default=None, ge=1, le=300)


@router.get("/config/alpaca-pnl-poll-interval", response_model=AlpacaPollIntervalOut)
def get_alpaca_poll_interval(
    _: User = Depends(require_admin),
) -> dict:
    from app.services.platform_config import get_alpaca_pnl_poll_interval_state
    return get_alpaca_pnl_poll_interval_state()


@router.patch("/config/alpaca-pnl-poll-interval", response_model=AlpacaPollIntervalOut)
def set_alpaca_poll_interval(
    payload: AlpacaPollIntervalIn,
    _: User = Depends(require_admin),
) -> dict:
    from app.services.platform_config import (
        get_alpaca_pnl_poll_interval_state,
        set_alpaca_pnl_poll_interval,
    )
    try:
        set_alpaca_pnl_poll_interval(payload.interval_s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.info(
        "admin set alpaca pnl_poll_interval_s override = %s",
        payload.interval_s,
    )
    return get_alpaca_pnl_poll_interval_state()


# ─── SMS test (Twilio) ────────────────────────────────────────────────────────

class TestSmsIn(BaseModel):
    # E.164, e.g. "+15551234567". Twilio validates the number itself; we only
    # do a light shape check so an obvious typo fails fast with a 422.
    to: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    body: str = Field(
        default="Test SMS from Kopyya — your Twilio Messaging Service works.",
        min_length=1,
        max_length=320,
    )


@router.post("/sms/test")
def send_test_sms(
    payload: TestSmsIn,
    _: User = Depends(require_admin),
) -> dict:
    """Fire a one-off SMS to confirm the Twilio credentials + Messaging Service
    are wired up. ``ok=false`` with no error usually means the creds are blank
    (the service no-ops in dev/QA) — check the logs for the exact reason."""
    from app.services.sms import send_sms  # noqa: PLC0415
    ok = send_sms(payload.to, payload.body)
    log.info("admin sent test SMS to=%s ok=%s", payload.to, ok)
    return {"ok": ok, "to": payload.to}
