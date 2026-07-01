"""Sprint 1 · Authentication · REGISTER  (POST /api/auth/register)

Covers AUTH-REG-* from the Phase 3 design: functional, business-rule, negative,
boundary, validation, privilege-escalation security, rate-limit, and DB checks.
"""
import pytest

from app.models.user import UserRole
from app.models.settings import SubscriberSettings, TraderSettings
from tests.helpers import audit_actions, fetch_user

VALID_PW = "Str0ng!pw"  # 8 chars, lower+upper+digit+symbol


def _reg(client, email, pw=VALID_PW, role="subscriber", business_name=None,
         display_name=None, ip="10.0.0.1", **extra):
    body = {"email": email, "password": pw, "role": role}
    if business_name is not None:
        body["business_name"] = business_name
    if display_name is not None:
        body["display_name"] = display_name
    body.update(extra)
    return client.post("/api/auth/register", json=body,
                       headers={"X-Forwarded-For": ip})


# --- Functional ------------------------------------------------------------
def test_register_subscriber_happy(client, db):  # AUTH-REG-FUNC-001
    r = _reg(client, "sub1@qatest.io")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "subscriber"
    assert body["email_verified"] is False
    row = db.query(SubscriberSettings).filter_by(user_id=body["id"]).one()
    assert row.copy_enabled is False and str(row.multiplier) == "1.000"
    assert "user.register" in audit_actions()


def test_register_trader_with_business_name(client, db):  # AUTH-REG-FUNC-002
    r = _reg(client, "trader1@qatest.io", role="trader", business_name="QA Capital")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "trader" and body["business_name"] == "QA Capital"
    assert db.query(TraderSettings).filter_by(user_id=body["id"]).one().trading_enabled is True


def test_multi_trader_allowed(client):  # AUTH-REG-FUNC-003 (README "one trader" is stale)
    assert _reg(client, "t1@qatest.io", role="trader", business_name="A").status_code == 201
    assert _reg(client, "t2@qatest.io", role="trader", business_name="B").status_code == 201


def test_register_display_name_optional(client):  # AUTH-REG-FUNC-005
    r = _reg(client, "sub2@qatest.io")
    assert r.status_code == 201 and r.json()["display_name"] is None


# --- Business rule ---------------------------------------------------------
def test_trader_requires_business_name(client):  # AUTH-REG-BIZ-001
    r = _reg(client, "trader2@qatest.io", role="trader")  # no business_name
    assert r.status_code == 422
    assert "business_name" in r.text


def test_subscriber_business_name_forced_null(client):  # AUTH-REG-BIZ-002
    r = _reg(client, "sub3@qatest.io", role="subscriber", business_name="ShouldVanish")
    assert r.status_code == 201
    assert r.json()["business_name"] is None


# --- Negative / duplicate --------------------------------------------------
def test_duplicate_email_conflict(client):  # AUTH-REG-NEG-001
    assert _reg(client, "dup@qatest.io").status_code == 201
    r = _reg(client, "dup@qatest.io")
    assert r.status_code == 409 and r.json()["detail"] == "email_taken"


def test_duplicate_email_case_insensitive(client):  # AUTH-REG-NEG-002
    assert _reg(client, "Case@qatest.io").status_code == 201
    # different case local-part should normalize to the same stored identity
    r = _reg(client, "CASE@qatest.io")
    assert r.status_code == 409


# --- Security: privilege escalation ---------------------------------------
def test_cannot_self_register_admin(client):  # AUTH-REG-SEC-001
    r = _reg(client, "evil@qatest.io", role="admin")
    assert r.status_code == 422
    assert fetch_user("evil@qatest.io") is None


def test_unknown_role_rejected(client):  # AUTH-REG-SEC-002
    assert _reg(client, "weird@qatest.io", role="superuser").status_code == 422


def test_register_ignores_mass_assignment(client):  # AUTH-REG-EXPL-002
    # is_active / email_verified are not schema fields → must be ignored, not honored.
    r = _reg(client, "mass@qatest.io", is_active=False, email_verified=True)
    assert r.status_code == 201
    u = fetch_user("mass@qatest.io")
    assert u.is_active is True and u.email_verified is False


# --- Password policy: boundary + validation -------------------------------
def test_password_min_ok(client):  # AUTH-REG-BND-001
    assert _reg(client, "p8@qatest.io", pw="Ab1!xyzz").status_code == 201


def test_password_too_short(client):  # AUTH-REG-BND-002
    assert _reg(client, "p7@qatest.io", pw="Ab1!xyz").status_code == 422


def test_password_needs_three_classes(client):  # AUTH-REG-BND-003
    # 8 chars but all lowercase → passes client minLength but server must reject.
    assert _reg(client, "weakpw@qatest.io", pw="aaaaaaaa").status_code == 422


def test_password_over_72_bytes(client):  # AUTH-REG-BND-004
    assert _reg(client, "long@qatest.io", pw="Ab1!" + "x" * 70).status_code == 422


def test_business_name_max_length(client):  # AUTH-REG-VAL-001
    assert _reg(client, "bn@qatest.io", role="trader",
                business_name="x" * 121).status_code == 422


def test_malformed_email_rejected(client):  # AUTH-REG-VAL-003
    assert _reg(client, "not-an-email").status_code == 422


def test_email_stored_normalized(client):  # AUTH-REG-VAL-004
    r = _reg(client, "  MixedCase@qatest.io  ")
    assert r.status_code == 201
    assert r.json()["email"] == "mixedcase@qatest.io"


# --- Rate limit ------------------------------------------------------------
def test_register_ip_throttle(client):  # AUTH-REG-BND-006
    ip = "203.0.113.7"
    last = None
    for i in range(17):  # limit is >15/hour
        last = _reg(client, f"rl{i}@qatest.io", ip=ip)
    assert last.status_code == 429
    assert last.headers.get("Retry-After")


# --- Database integrity ----------------------------------------------------
def test_password_hashed_not_plaintext(client):  # AUTH-REG-DB-002
    _reg(client, "hash@qatest.io", pw=VALID_PW)
    u = fetch_user("hash@qatest.io")
    assert u.password_hash != VALID_PW and u.password_hash.startswith("$2")
