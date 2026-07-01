"""Sprint 1 · Authentication · cross-cutting API SECURITY
Covers AUTH-LOGIN-SEC-*, AUTH-X-SEC-* : JWT forgery, alg:none, expiry,
SQLi, CORS, generic errors.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

from jose import jwt

from app.config import get_settings
from app.models.user import UserRole
from tests.helpers import make_user, access_token_for


def _b64url(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_forged_token_wrong_secret_rejected(client, db):  # AUTH-LOGIN-SEC-001
    u = make_user(db, "forge@qatest.io")
    forged = jwt.encode(
        {"sub": str(u.id), "role": "admin", "type": "access",
         "exp": datetime.now(timezone.utc) + timedelta(minutes=30)},
        "attacker-does-not-know-the-secret", algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_alg_none_rejected(client, db):  # AUTH-LOGIN-SEC-002
    # jose refuses to *encode* alg=none, so craft the unsigned token by hand
    # (header.payload. with an empty signature) — the classic alg-none attack.
    u = make_user(db, "algnone@qatest.io")
    token = (
        _b64url({"alg": "none", "typ": "JWT"})
        + "." + _b64url({"sub": str(u.id), "role": "admin", "type": "access"})
        + "."
    )
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_access_rejected(client, db):  # AUTH-LOGIN-SEC-003
    u = make_user(db, "expacc@qatest.io")
    s = get_settings()
    expired = jwt.encode(
        {"sub": str(u.id), "role": u.role.value, "type": "access",
         "iat": datetime.now(timezone.utc) - timedelta(hours=2),
         "exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        s.jwt_secret, algorithm=s.jwt_algorithm)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_sqli_login_no_bypass(client, db):  # AUTH-LOGIN-SEC-005
    make_user(db, "sqli@qatest.io")
    r = client.post("/api/auth/login",
                    json={"email": "sqli@qatest.io", "password": "' OR '1'='1"})
    assert r.status_code == 401  # not authenticated, not 500


def test_cors_allowed_origin_echoed(client, db):  # AUTH-X-SEC-001
    make_user(db, "cors@qatest.io")
    r = client.post("/api/auth/login",
                    json={"email": "cors@qatest.io", "password": "x"},
                    headers={"Origin": "http://localhost:3000"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_unknown_origin_not_allowed(client, db):  # AUTH-X-SEC-001
    make_user(db, "cors2@qatest.io")
    r = client.post("/api/auth/login",
                    json={"email": "cors2@qatest.io", "password": "x"},
                    headers={"Origin": "http://evil.example"})
    assert r.headers.get("access-control-allow-origin") != "http://evil.example"


def test_error_body_is_generic_json(client):  # AUTH-X-SEC-003
    r = client.post("/api/auth/login", json={})  # validation error
    assert r.status_code == 422
    assert "traceback" not in r.text.lower() and "sqlalchemy" not in r.text.lower()
