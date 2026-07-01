"""Sprint 1 · Authentication · LOGIN  (POST /api/auth/login)

Covers AUTH-LOGIN-* : functional, business-rule, negative, boundary (lockout),
and the token-pair contract.
"""
import pytest
from jose import jwt

from app.config import get_settings
from app.core.security import decode_token, hash_password
from app.models.user import User, UserRole
from tests.helpers import make_user, audit_actions, DEFAULT_PW


def _login(client, email, pw=DEFAULT_PW, ip="10.0.0.2"):
    return client.post("/api/auth/login", json={"email": email, "password": pw},
                       headers={"X-Forwarded-For": ip})


# --- Functional ------------------------------------------------------------
def test_login_trader_returns_tokenpair(client, db):  # AUTH-LOGIN-FUNC-001
    make_user(db, "trader@qatest.io", role=UserRole.TRADER, business_name="QA")
    r = _login(client, "trader@qatest.io")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    claims = decode_token(body["access_token"])
    assert claims["role"] == "trader" and claims["type"] == "access"
    assert "user.login" in audit_actions()


def test_login_subscriber(client, db):  # AUTH-LOGIN-FUNC-002
    make_user(db, "sub@qatest.io", role=UserRole.SUBSCRIBER)
    assert decode_token(_login(client, "sub@qatest.io").json()["access_token"])["role"] == "subscriber"


@pytest.mark.candidate_bug
def test_login_admin(client, db):  # AUTH-LOGIN-FUNC-003 / BUG-AUTH-001
    """Admin login requires an admin user. The user_role enum stores 'admin'
    lowercase while TRADER/SUBSCRIBER are stored as their uppercase member
    names, so the ORM can neither create nor read an admin row. This SHOULD
    succeed once the enum label is fixed."""
    try:
        u = User(email="admin@qatest.io", password_hash=hash_password(DEFAULT_PW),
                 role=UserRole.ADMIN, is_active=True)
        db.add(u)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        pytest.fail(f"cannot create admin via ORM (enum mismatch, BUG-AUTH-001): {type(e).__name__}")
    assert decode_token(_login(client, "admin@qatest.io").json()["access_token"])["role"] == "admin"


def test_login_email_case_insensitive(client, db):  # AUTH-LOGIN-FUNC-005
    make_user(db, "case@qatest.io")
    assert _login(client, "CASE@qatest.io").status_code == 200


def test_login_email_trimmed(client, db):  # AUTH-LOGIN-FUNC-006
    make_user(db, "trim@qatest.io")
    assert _login(client, "  trim@qatest.io  ").status_code == 200


# --- Business rule ---------------------------------------------------------
def test_unverified_user_can_login(client, db):  # AUTH-LOGIN-BIZ-001 (soft verify)
    make_user(db, "unverified@qatest.io", email_verified=False)
    assert _login(client, "unverified@qatest.io").status_code == 200


def test_inactive_user_blocked(client, db):  # AUTH-LOGIN-BIZ-002
    make_user(db, "inactive@qatest.io", is_active=False)
    r = _login(client, "inactive@qatest.io")
    assert r.status_code == 403 and r.json()["detail"] == "user_inactive"


# --- Negative --------------------------------------------------------------
def test_wrong_password(client, db):  # AUTH-LOGIN-NEG-001
    make_user(db, "wp@qatest.io")
    r = _login(client, "wp@qatest.io", pw="WrongPass1!")
    assert r.status_code == 401 and r.json()["detail"] == "invalid_credentials"
    assert "user.login_failed" in audit_actions()


def test_unknown_email_same_401(client):  # AUTH-LOGIN-NEG-002 (no enumeration)
    r = _login(client, "ghost@qatest.io")
    assert r.status_code == 401 and r.json()["detail"] == "invalid_credentials"


def test_missing_password(client):  # AUTH-LOGIN-NEG-003
    assert client.post("/api/auth/login", json={"email": "x@qatest.io"}).status_code == 422


def test_malformed_email(client):  # AUTH-LOGIN-NEG-004
    assert _login(client, "nope").status_code == 422


def test_empty_body(client):  # AUTH-LOGIN-NEG-005
    assert client.post("/api/auth/login", json={}).status_code == 422


# --- Boundary: brute-force lockout ----------------------------------------
def test_email_lockout_after_8_failures(client, db):  # AUTH-LOGIN-BND-001
    make_user(db, "lock@qatest.io")
    for _ in range(8):
        assert _login(client, "lock@qatest.io", pw="Bad1!pwd").status_code == 401
    r = _login(client, "lock@qatest.io", pw="Bad1!pwd")  # 9th
    assert r.status_code == 429 and r.json()["detail"] == "too_many_attempts"
    assert r.headers.get("Retry-After") == "900"
    # Even the CORRECT password is now rejected while locked.
    assert _login(client, "lock@qatest.io").status_code == 429


def test_success_resets_failure_counter(client, db):  # AUTH-LOGIN-BND-003
    make_user(db, "reset@qatest.io")
    for _ in range(7):
        _login(client, "reset@qatest.io", pw="Bad1!pwd")
    assert _login(client, "reset@qatest.io").status_code == 200  # clears counter
    for _ in range(7):
        _login(client, "reset@qatest.io", pw="Bad1!pwd")
    # would have locked at 8 cumulative; counter was reset, so still not locked
    assert _login(client, "reset@qatest.io").status_code == 200


def test_ip_throttle_over_40(client):  # AUTH-LOGIN-BND-002
    ip = "198.51.100.9"
    last = None
    for i in range(42):
        last = _login(client, f"ipx{i}@qatest.io", pw="Bad1!pwd", ip=ip)
    assert last.status_code == 429
