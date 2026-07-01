"""Sprint 1 · Authentication · REFRESH + ME
(POST /api/auth/refresh, GET /api/auth/me)

Covers AUTH-LOGIN-SESS-003, AUTH-LOGIN-AUTHZ-*.
"""
from app.core.security import decode_token
from app.models.user import UserRole
from tests.helpers import (
    make_user, refresh_token_for, access_token_for, auth_header,
)


# --- /refresh --------------------------------------------------------------
def test_refresh_returns_new_pair(client, db):  # AUTH-LOGIN-SESS-003
    u = make_user(db, "rf@qatest.io", role=UserRole.TRADER, business_name="QA")
    r = client.post("/api/auth/refresh", json={"refresh_token": refresh_token_for(u)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert decode_token(body["access_token"])["type"] == "access"
    assert decode_token(body["access_token"])["role"] == "trader"


def test_refresh_rejects_access_token(client, db):  # wrong type
    u = make_user(db, "rf2@qatest.io")
    r = client.post("/api/auth/refresh", json={"refresh_token": access_token_for(u)})
    assert r.status_code == 401 and r.json()["detail"] == "wrong_token_type"


def test_refresh_garbage(client):
    r = client.post("/api/auth/refresh", json={"refresh_token": "nope"})
    assert r.status_code == 401 and r.json()["detail"] == "invalid_token"


def test_refresh_inactive_user(client, db):
    u = make_user(db, "rfi@qatest.io", is_active=False)
    r = client.post("/api/auth/refresh", json={"refresh_token": refresh_token_for(u)})
    assert r.status_code == 401 and r.json()["detail"] == "user_inactive"


# --- /me -------------------------------------------------------------------
def test_me_no_token(client):  # AUTH-LOGIN-AUTHZ-001
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_self(client, db):  # AUTH-LOGIN-AUTHZ-002
    u = make_user(db, "me@qatest.io", role=UserRole.SUBSCRIBER)
    r = client.get("/api/auth/me", headers=auth_header(u))
    assert r.status_code == 200
    assert r.json()["email"] == "me@qatest.io" and r.json()["role"] == "subscriber"


def test_me_rejects_refresh_token(client, db):  # wrong token type on /me
    u = make_user(db, "me2@qatest.io")
    r = client.get("/api/auth/me",
                   headers={"Authorization": f"Bearer {refresh_token_for(u)}"})
    assert r.status_code == 401 and r.json()["detail"] == "wrong_token_type"
