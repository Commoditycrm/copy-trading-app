"""Sanity checks that the test harness itself is wired correctly."""
from app.models.user import User, UserRole
from tests.helpers import make_user, DEFAULT_PW


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_db_roundtrip_and_isolation(client, db):
    make_user(db, "harness@qatest.io", role=UserRole.SUBSCRIBER)
    assert db.query(User).filter(User.email == "harness@qatest.io").count() == 1


def test_isolation_truncated_between_tests(db):
    # The user from the previous test must be gone (autouse _clean ran).
    assert db.query(User).count() == 0


def test_login_via_api(client, db):
    make_user(db, "harness2@qatest.io", role=UserRole.SUBSCRIBER)
    r = client.post(
        "/api/auth/login",
        json={"email": "harness2@qatest.io", "password": DEFAULT_PW},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
