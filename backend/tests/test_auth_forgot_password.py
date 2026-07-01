"""Sprint 1 · Authentication · FORGOT PASSWORD  (POST /api/auth/forgot-password)

Covers AUTH-FORGOT-* : anti-enumeration, audit behavior, and the W1
email-normalization watchlist item.
"""
import pytest

from app.models.user import UserRole
from tests.helpers import make_user, audit_actions

GENERIC = "If an account with that email exists, a password reset link has been sent."


def _forgot(client, email):
    return client.post("/api/auth/forgot-password", json={"email": email})


def test_existing_user_gets_reset(client, db):  # AUTH-FORGOT-FUNC-001
    make_user(db, "fp@qatest.io")
    r = _forgot(client, "fp@qatest.io")
    assert r.status_code == 200 and r.json()["detail"] == GENERIC
    assert "user.password_reset_requested" in audit_actions()


def test_nonexistent_email_no_audit(client):  # AUTH-FORGOT-BIZ-001
    r = _forgot(client, "ghost@qatest.io")
    assert r.status_code == 200 and r.json()["detail"] == GENERIC
    assert "user.password_reset_requested" not in audit_actions()


def test_inactive_user_no_token(client, db):  # AUTH-FORGOT-BIZ-002
    make_user(db, "inactivefp@qatest.io", is_active=False)
    r = _forgot(client, "inactivefp@qatest.io")
    assert r.status_code == 200
    assert "user.password_reset_requested" not in audit_actions()


def test_response_identical_existing_vs_not(client, db):  # AUTH-FORGOT-SEC-001
    make_user(db, "real@qatest.io")
    a = _forgot(client, "real@qatest.io")
    b = _forgot(client, "fake@qatest.io")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_malformed_email(client):  # AUTH-FORGOT-NEG-001
    assert _forgot(client, "bogus").status_code == 422


@pytest.mark.candidate_bug
def test_mixed_case_local_part_still_sends(client, db):  # AUTH-FORGOT-NEG-003 (W1)
    """User stored lowercased; requesting reset with a mixed-case local-part
    SHOULD still find them (email is a case-insensitive identity). If the
    endpoint doesn't normalize, no reset is sent → finding."""
    make_user(db, "person@qatest.io")
    r = _forgot(client, "Person@qatest.io")
    assert r.status_code == 200
    assert "user.password_reset_requested" in audit_actions(), (
        "forgot-password did not normalize the email local-part (W1)"
    )
