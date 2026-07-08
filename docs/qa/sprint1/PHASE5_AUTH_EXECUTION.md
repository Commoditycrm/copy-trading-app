# Phase 5 — Test Execution Report: Sprint 1 · Authentication (API/Integration layer)

**Project:** Copy-Trading Platform · **Module:** Authentication
**Date:** 2026-06-29 · **Branch:** `qa-branch` · **Backend:** 0.2.0
**Environment:** LOCAL only — FastAPI TestClient (in-process), Postgres `trading_app_test` @ `127.0.0.1:5433`, Redis @ `127.0.0.1:6380/1`, `RUN_BACKGROUND_WORKERS=false`.
**Suite:** `backend/tests/` · **Artifact:** `auth_junit.xml`

> Production was NOT touched. Live-broker / SendGrid sends were not exercised (tokens minted in-process).

---

## Scope of this run

This round automated and executed the **API / Database / Security / Session / Authorization**
layers for all five auth features (Login, Register, Forgot, Reset, Verify) plus `/refresh` and `/me`.

**Not yet executed (next automation round):** UI E2E (Playwright), Accessibility (axe-core),
Responsive, Cross-browser, and Performance (k6/Lighthouse). These require the frontend wired to the
backend and are planned after the Phase 6 fixes are approved (or in parallel, your call).

---

## Results

| Metric | Count |
|---|---|
| Test cases executed | **77** (incl. 4 harness self-checks) |
| Passed | **73** |
| Failed | **4** (all confirmed defects — see bug report) |
| Blocked / Skipped | 0 |
| Duration | 212 s (bcrypt-bound) |

**Pass rate (auth):** 69/73 functional auth cases pass; the 4 failures are genuine product defects,
not test errors. Coverage of the executed layers is effectively complete for the designed cases.

### By feature
| Feature | Executed | Pass | Fail | Failing tests |
|---|---|---|---|---|
| Register | 18 | 18 | 0 | — |
| Login | 14 | 13 | 1 | `test_login_admin` (BUG-AUTH-001) |
| Forgot Password | 6 | 5 | 1 | `test_mixed_case_local_part_still_sends` (BUG-AUTH-002) |
| Reset Password | 9 | 7 | 2 | `test_reset_enforces_complexity`, `test_reset_caps_at_72_bytes` (BUG-AUTH-003) |
| Verify + Resend | 9 | 9 | 0 | — |
| Refresh + Me | 8 | 8 | 0 | — |
| Cross-cutting security | 7 | 7 | 0 | — |
| Harness self-checks | 4 | 4 | 0 | — |

---

## Failures (full RCA in [PHASE6_AUTH_BUG_REPORT.md](PHASE6_AUTH_BUG_REPORT.md))

| Test | Maps to | Severity | One-line |
|---|---|---|---|
| `test_login_admin` | BUG-AUTH-001 | **Critical** | `admin` enum stored lowercase vs uppercase member-name → ORM can't create/read admins (proven: `DataError` on insert, `LookupError` on read). Blocks Admin module. |
| `test_mixed_case_local_part_still_sends` | BUG-AUTH-002 | **High** | forgot-password/resend don't normalize email → mixed-case users get no reset/verify mail (silent, 200 returned). |
| `test_reset_enforces_complexity` | BUG-AUTH-003 | **Medium** | reset-password accepts an all-lowercase password register would reject. |
| `test_reset_caps_at_72_bytes` | BUG-AUTH-003 | **Medium** | reset-password accepts >72-byte password (bcrypt silently truncates). |

**Server-log observation (non-failing):** repeated `error reading bcrypt version` (passlib 1.7.4 + bcrypt 4.x) — OBS-ENV-001, cosmetic.

---

## Evidence

- JUnit XML: `docs/qa/sprint1/auth_junit.xml`
- Direct ORM probe confirming BUG-AUTH-001 both directions (insert → `DataError: invalid input value for enum user_role: "ADMIN"`; read of lowercase `admin` → `LookupError: 'admin' is not among the defined enum values`).
- All negative/security expectations (JWT forgery, `alg:none`, expiry, SQLi, CORS, lockout, single-use reset, idempotent verify, mass-assignment) **passed**.

---

## Exit-criteria status (this layer)

| Criterion | Status |
|---|---|
| 100% designed API/DB/security cases executed | ✅ |
| 0 open Critical/High | ❌ — BUG-AUTH-001 (Critical), BUG-AUTH-002 (High) open, awaiting fix approval |
| UI/a11y/perf executed | ⏳ pending next automation round |

**Module is NOT yet exit-ready** — gated on Phase 6 fixes (approval required) + the pending E2E/a11y/perf round, then Phase 7 regression.

---

*Next: your approval on the bug report → fix approved items only → Phase 7 regression re-run.*
