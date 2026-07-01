"""Sprint 1 · Authentication · RESET PASSWORD  (POST /api/auth/reset-password)

Covers AUTH-RESET-* : single-use semantics, token validation, and the W2
policy-gap watchlist items.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.config import get_settings
from app.core.security import _password_fingerprint
from tests.helpers import make_user, reset_token_for, access_token_for, audit_actions, DEFAULT_PW


def _reset(client, token, new_pw):
    return client.post("/api/auth/reset-password",
                       json={"token": token, "new_password": new_pw})


def _login(client, email, pw):
    return client.post("/api/auth/login", json={"email": email, "password": pw})


def test_valid_reset_changes_password(client, db):  # AUTH-RESET-FUNC-001
    u = make_user(db, "rp@qatest.io")
    r = _reset(client, reset_token_for(u), "NewStr0ng!1")
    assert r.status_code == 200, r.text
    assert _login(client, "rp@qatest.io", "NewStr0ng!1").status_code == 200
    assert _login(client, "rp@qatest.io", DEFAULT_PW).status_code == 401  # old pw dead
    assert "user.password_reset" in audit_actions()


def test_token_single_use(client, db):  # AUTH-RESET-BIZ-001
    u = make_user(db, "single@qatest.io")
    tok = reset_token_for(u)
    assert _reset(client, tok, "FirstNew!1a").status_code == 200
    # fingerprint no longer matches the (now changed) hash → reuse fails
    assert _reset(client, tok, "SecondNew!2b").status_code == 400


def test_tampered_token(client):  # AUTH-RESET-NEG-001
    assert _reset(client, "garbage.token.value", "Whatever!1a").status_code == 400


def test_wrong_token_type_rejected(client, db):  # AUTH-RESET-NEG-003
    u = make_user(db, "wt@qatest.io")
    assert _reset(client, access_token_for(u), "Whatever!1a").status_code == 400


def test_expired_token_rejected(client, db):  # AUTH-RESET-NEG-002
    u = make_user(db, "exp@qatest.io")
    s = get_settings()
    expired = jwt.encode(
        {"sub": str(u.id), "type": "reset", "pwf": _password_fingerprint(u.password_hash),
         "iat": datetime.now(timezone.utc) - timedelta(hours=2),
         "exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        s.jwt_secret, algorithm=s.jwt_algorithm)
    assert _reset(client, expired, "Whatever!1a").status_code == 400


def test_missing_token_field(client):  # AUTH-RESET-NEG-005
    assert client.post("/api/auth/reset-password",
                       json={"new_password": "Whatever!1a"}).status_code == 422


def test_new_password_too_short(client, db):  # AUTH-RESET-BND-001
    u = make_user(db, "short@qatest.io")
    assert _reset(client, reset_token_for(u), "Ab1!xyz").status_code == 422


@pytest.mark.candidate_bug
def test_reset_enforces_complexity(client, db):  # AUTH-RESET-SEC-003 (W2)
    """Reset SHOULD enforce the same complexity policy as register. Currently
    ResetPasswordIn only checks length → an all-lowercase password is accepted."""
    u = make_user(db, "weakreset@qatest.io")
    r = _reset(client, reset_token_for(u), "aaaaaaaa")
    assert r.status_code == 422, "reset-password accepted a password register would reject (W2)"


@pytest.mark.candidate_bug
def test_reset_caps_at_72_bytes(client, db):  # AUTH-RESET-BND-002 (W2)
    """Register caps passwords at 72 bytes (bcrypt limit); reset allows up to
    128, so >72 bytes is silently truncated. Expect parity (reject >72)."""
    u = make_user(db, "longreset@qatest.io")
    r = _reset(client, reset_token_for(u), "Ab1!" + "x" * 80)
    assert r.status_code == 422, "reset-password accepts >72-byte password (bcrypt truncates) (W2)"
