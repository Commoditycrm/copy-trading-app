"""Sprint 1 · Authentication · VERIFY EMAIL + RESEND
(POST /api/auth/verify-email, /api/auth/resend-verification)

Covers AUTH-VERIFY-* : happy path, idempotency, token validation, eml binding,
and resend anti-enumeration.
"""
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt
from sqlalchemy import text

from app.config import get_settings
from app.models.user import UserRole
from tests.helpers import (
    make_user, verify_token_for, access_token_for, audit_actions, fetch_user,
)
from app.database import engine


def _verify(client, token):
    return client.post("/api/auth/verify-email", json={"token": token})


def _resend(client, email):
    return client.post("/api/auth/resend-verification", json={"email": email})


def test_valid_token_verifies(client, db):  # AUTH-VERIFY-FUNC-001
    u = make_user(db, "ve@qatest.io", email_verified=False)
    r = _verify(client, verify_token_for(u))
    assert r.status_code == 200, r.text
    fresh = fetch_user("ve@qatest.io")
    assert fresh.email_verified is True and fresh.email_verified_at is not None
    assert "user.email_verified" in audit_actions()


def test_verify_idempotent(client, db):  # AUTH-VERIFY-BIZ-001
    u = make_user(db, "idem@qatest.io", email_verified=False)
    tok = verify_token_for(u)
    assert _verify(client, tok).status_code == 200
    assert _verify(client, tok).status_code == 200  # second time still 200
    # only one audit row written (idempotent path skips re-audit)
    assert audit_actions().count("user.email_verified") == 1


def test_tampered_token(client):  # AUTH-VERIFY-NEG-001
    assert _verify(client, "not.a.real.token").status_code == 400


def test_wrong_token_type(client, db):  # AUTH-VERIFY-NEG-003
    u = make_user(db, "wtt@qatest.io")
    assert _verify(client, access_token_for(u)).status_code == 400


def test_expired_token(client, db):  # AUTH-VERIFY-NEG-002
    u = make_user(db, "vexp@qatest.io", email_verified=False)
    s = get_settings()
    expired = jwt.encode(
        {"sub": str(u.id), "type": "verify", "eml": u.email,
         "iat": datetime.now(timezone.utc) - timedelta(days=2),
         "exp": datetime.now(timezone.utc) - timedelta(days=1)},
        s.jwt_secret, algorithm=s.jwt_algorithm)
    assert _verify(client, expired).status_code == 400


def test_eml_mismatch_after_email_change(client, db):  # AUTH-VERIFY-NEG-004
    u = make_user(db, "old@qatest.io", email_verified=False)
    tok = verify_token_for(u)  # bound to old@
    with engine.begin() as c:
        c.execute(text("UPDATE users SET email='new@qatest.io' WHERE id=:i"), {"i": str(u.id)})
    assert _verify(client, tok).status_code == 400  # eml no longer matches


# --- Resend ----------------------------------------------------------------
def test_resend_unverified(client, db):  # AUTH-VERIFY-BIZ-002a
    make_user(db, "resend@qatest.io", email_verified=False)
    assert _resend(client, "resend@qatest.io").status_code == 200
    assert "user.verification_resent" in audit_actions()


def test_resend_already_verified_no_send(client, db):  # AUTH-VERIFY-BIZ-002b
    make_user(db, "done@qatest.io", email_verified=True)
    assert _resend(client, "done@qatest.io").status_code == 200
    assert "user.verification_resent" not in audit_actions()


def test_resend_nonexistent_no_leak(client):  # anti-enumeration
    assert _resend(client, "ghost@qatest.io").status_code == 200
    assert "user.verification_resent" not in audit_actions()
