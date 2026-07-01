# Phase 6 — Bug Report: Sprint 1 · Authentication

**Project:** Copy-Trading Platform · **Module:** Authentication
**Environment:** LOCAL (backend in-process via FastAPI TestClient; Postgres `trading_app_test` @ :5433; Redis @ :6380/1)
**Branch:** `qa-branch` · **Date:** 2026-06-29
**Suite:** `backend/tests/` (auth) — see [PHASE5_AUTH_EXECUTION.md](PHASE5_AUTH_EXECUTION.md) for the run.

> ⚠️ No code has been changed. These are reported for your approval before any fix (Phase 6 rule).
> Production caveat: findings are derived from code + migrations as source of truth on `qa-branch`.
> You own production — please confirm against the live DB where noted.

---

## Summary

| ID | Title | Severity | Confidence | Type |
|---|---|---|---|---|
| **BUG-AUTH-001** | `admin` role enum label mismatch — admin role unusable via ORM | **Critical** | High (proven) | Functional / Data |
| **BUG-AUTH-002** | `forgot-password` & `resend-verification` don't normalize email → silent no-send for mixed-case | **High** | High (proven) | Functional / Security |
| **BUG-AUTH-003** | `reset-password` enforces a weaker password policy than registration | **Medium** | High (proven) | Security |
| **BUG-AUTH-004** | Duplicate/dead class definitions in `schemas/auth.py` | **Low** | High | Maintainability |
| **OBS-ENV-001** | passlib 1.7.4 + bcrypt 4.x version-read warning floods logs | **Low** | High | Dependency/Ops |

---

## BUG-AUTH-001 — `admin` role enum label mismatch (Critical)

**Feature:** Roles / Admin login / `PATCH /api/admin/users/{id}/role`
**Test:** `tests/test_auth_login.py::test_login_admin` (also surfaced by direct ORM probe)

**Steps to reproduce**
1. Apply migrations to a fresh DB (`alembic upgrade head`).
2. Attempt to create an admin via the ORM: `User(role=UserRole.ADMIN)` → `db.commit()`.
3. Also: raw-insert a row with `role='admin'` and read it back via the ORM.

**Expected:** Admin users can be created/promoted and read (admin login works; Admin module usable).

**Actual (proven):**
- ORM **insert** `ADMIN` → `psycopg.errors.InvalidTextRepresentation: invalid input value for enum user_role: "ADMIN"` (500 DataError).
- ORM **read** of a lowercase `admin` row → `LookupError: 'admin' is not among the defined enum values ... Possible values: TRADER, SUBSCRIBER`.

**Root cause**
`app/models/user.py` maps `Enum(UserRole, name="user_role")` with **no `values_callable`**, so SQLAlchemy persists the **member name** — `TRADER`, `SUBSCRIBER`, `ADMIN` (uppercase). The original enum type was created with those uppercase labels, but the later migration added the admin label **lowercase**:

`alembic/versions/f1a2b3c4d5e6_add_admin_role.py:26`
```python
op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'admin'")   # ← should be 'ADMIN'
```
So the DB enum is `{TRADER, SUBSCRIBER, admin}` — the ORM emits/reads `ADMIN`, which doesn't exist.

**Impact:** The entire **Admin** role is non-functional through the application: admin promotion (`PATCH /api/admin/users/{id}/role`) 500s, admin login can't work, and any admin row breaks ORM reads. Blocks **Sprint 7 (Admin module)** end-to-end.

**Affected files:** `backend/alembic/versions/f1a2b3c4d5e6_add_admin_role.py`, `backend/app/models/user.py`
**Suggested fix (for approval):** make the labels consistent — either (a) a new migration that renames the value (`ALTER TYPE user_role RENAME VALUE 'admin' TO 'ADMIN'`, PG ≥10), or (b) standardize the enum on `.value` (lowercase) across the board via `values_callable=lambda e: [m.value for m in e]` **plus** a data/enum migration to lowercase `TRADER`/`SUBSCRIBER`. Option (a) is the minimal change. **Verify production's actual enum labels first.**

---

## BUG-AUTH-002 — forgot-password / resend not normalized (High)

**Feature:** Password reset & verification resend
**Test:** `tests/test_auth_forgot_password.py::test_mixed_case_local_part_still_sends`

**Steps to reproduce**
1. User exists as `person@qatest.io` (registration always lowercases, so the stored email is lowercase).
2. `POST /api/auth/forgot-password {"email": "Person@qatest.io"}` (capitalized local-part — as a user would type their name).

**Expected:** Email treated case-insensitively → reset link sent (audit `user.password_reset_requested` written).

**Actual:** Returns the generic 200, but **no reset is sent** and no audit row is written. `ForgotPasswordIn` (and `ResendVerificationIn`) lack the `_normalize_email` before-validator that `LoginIn`/`RegisterIn` have; `EmailStr` lowercases only the **domain**, not the local-part, so `Person@…` ≠ stored `person@…`.

**Impact:** Any user who types capitals in their email **cannot recover their password** or resend verification — and the anti-enumeration design hides the failure (UI says "if an account exists, we've sent…"). Login still works (it normalizes), so the inconsistency is confusing. Account-recovery reliability issue.

**Affected file:** `backend/app/schemas/auth.py` (`ForgotPasswordIn`, `ResendVerificationIn`)
**Suggested fix:** add `_norm_email = field_validator("email", mode="before")(_normalize_email)` to both schemas (and consider the frontend forgot-password input mirroring login's lowercase-on-keystroke).

---

## BUG-AUTH-003 — reset-password weaker than register (Medium)

**Feature:** Reset password policy
**Tests:** `tests/test_auth_reset_password.py::test_reset_enforces_complexity`, `::test_reset_caps_at_72_bytes`

**Steps to reproduce**
1. Mint a valid reset token for a user.
2. `POST /api/auth/reset-password` with `new_password="aaaaaaaa"` (8 lowercase) → **200** (accepted).
3. With an 84-char password (>72 bytes) → **200** (accepted; bcrypt silently truncates at 72 bytes).

**Expected:** Reset enforces the **same** policy as registration: ≥3 of {lower, upper, digit, symbol} and ≤72 bytes (so a reset can't set a weaker or silently-truncated password).

**Actual:** `ResetPasswordIn` only checks `min_length=8, max_length=128` — no complexity rule, no 72-byte cap. A user can reset to a weak password that registration would reject, and >72-byte passwords are truncated behind the user's back.

**Affected file:** `backend/app/schemas/auth.py` (`ResetPasswordIn`)
**Suggested fix:** reuse `_validate_password_strength` on `new_password` and set `max_length=72` (matching `RegisterIn`).

---

## BUG-AUTH-004 — duplicate/dead definitions in schemas/auth.py (Low)

`backend/app/schemas/auth.py` defines `ForgotPasswordIn`, `ResetPasswordIn`, and `MessageOut` **3–4 times each** (lines ~101–171). Python keeps the last definition; the earlier ones are dead code and a maintenance/merge hazard (e.g., it's easy to "fix" a duplicate that isn't the one in effect).
**Suggested fix:** delete the redundant definitions, keeping one of each.

---

## OBS-ENV-001 — bcrypt version-read warning (Low, environment)

passlib 1.7.4 calls `bcrypt.__about__.__version__`, which bcrypt 4.x removed → repeated `(trapped) error reading bcrypt version` / `AttributeError: module 'bcrypt' has no attribute '__about__'` in logs. **Non-fatal** — hashing/verification work (all password tests pass). Cosmetic log noise + a signal the pinned passlib/bcrypt pair is mismatched.
**Suggested fix (optional):** pin `bcrypt<4.1`/`==4.0.1` or upgrade passlib, to quiet logs.

---

## Not bugs (verified correct behavior — for the record)

- Multi-trader registration is allowed (the README's "only one trader" note is **stale**, not a defect).
- Post-login landing is admin→`/admin`, trader/subscriber→`/dashboard` (login page's `/trade-panel`/`/trades` comment is stale).
- Email anti-enumeration (forgot/resend return identical responses), JWT signature/`alg:none`/expiry rejection, brute-force lockout (8/email, 40/IP), single-use reset tokens, idempotent verify, mass-assignment ignored — all behave correctly.

---

*Awaiting approval. On your go-ahead I will fix ONLY the approved items from this list, keep changes minimal, then run Phase 7 regression.*
