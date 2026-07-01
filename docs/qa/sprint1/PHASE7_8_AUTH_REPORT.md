# Phase 7 (Regression) + Phase 8 (QA Report) — Sprint 1 · Authentication

**Project:** Copy-Trading Platform · **Module:** Authentication
**Date:** 2026-06-29 · **Branch:** `qa-branch` · **App version:** backend 0.2.0
**Environment:** LOCAL — FastAPI TestClient (in-process), Postgres `trading_app_test`@5433, Redis@6380/1, `RUN_BACKGROUND_WORKERS=false`
**Layer:** API / Integration / Database / Security (UI E2E + a11y + perf = next round)

---

## Executive Summary

The Authentication module's API/DB/security layer was designed (~110 cases), automated (77 executed
in `backend/tests/`), and executed against a local stack. The first run surfaced **4 defects**
(1 Critical, 1 High, 2 Medium). All four were fixed (approved) and **Phase 7 regression is green:
77/77 pass, 0 regressions.** Backend auth is solid; the UI/accessibility/performance layer remains
to be built before the module is fully signed off.

---

## Phase 7 — Regression

| Run | Result | Notes |
|---|---|---|
| Initial execution (pre-fix) | 73 pass / 4 fail | 4 failures = confirmed defects |
| Post-fix regression | **77 pass / 0 fail** (109 s) | all 4 fixed, no new failures |

> A regression attempt in between returned 77 *errors* — root-caused to the Docker DB containers
> being torn down between sessions (infra loss), not code. Re-run on a rebuilt stack: clean.

Each previously-failing test now asserts the corrected behavior and passes:
- `test_login_admin` — admin create/read via ORM works.
- `test_mixed_case_local_part_still_sends` — mixed-case forgot-password now sends (audit row written).
- `test_reset_enforces_complexity` — weak reset password now rejected (422).
- `test_reset_caps_at_72_bytes` — >72-byte reset password now rejected (422).

---

## Phase 8 — QA Report

### Test execution

| Metric | Count |
|---|---|
| Designed cases (all 21 types) | ~110 |
| Automated + executed (API/DB/Sec layer) | 77 |
| Passed | 77 |
| Failed / Blocked / Skipped | 0 / 0 / 0 |
| Defects found | 4 (all fixed + verified) |

**Coverage:** API/DB/Session/Authorization/Security layer — effectively complete for designed cases.
**Not yet covered:** UI E2E (Playwright), Accessibility (axe), Responsive, Cross-browser,
Performance (k6/Lighthouse) — planned as the next automation round under the agreed `/qa` area.

### Bug summary

| ID | Severity | Title | Status |
|---|---|---|---|
| BUG-AUTH-001 | Critical | `admin` enum label case mismatch blocked all admin ORM ops | ✅ Fixed (migration `c4f1a9d3e7b2`) |
| BUG-AUTH-002 | High | forgot-password/resend didn't normalize email → silent no-send | ✅ Fixed (schema normalizer) |
| BUG-AUTH-003 | Medium | reset-password weaker than register (no complexity, >72 bytes) | ✅ Fixed (policy parity) |
| BUG-AUTH-004 | Low | duplicate schema class definitions | ✅ Fixed (deduped) |
| OBS-ENV-001 | Low | bcrypt `__about__` log spam | ✅ Fixed (`bcrypt==4.0.1`) |

### Findings by area

- **Security (validated, no defects):** JWT signature forgery rejected; `alg:none` rejected; expired
  tokens rejected; SQLi in credentials → no bypass; brute-force lockout (8/email, 40/IP) enforced;
  anti-enumeration on forgot/login; single-use reset tokens; mass-assignment of `is_active`/
  `email_verified` ignored; CORS origin allow-list enforced. **One accepted risk:** rate-limiting is
  fail-open if Redis is down (documented design trade-off — monitor Redis availability in prod).
- **API/Business rules (validated):** multi-trader supported (README stale); soft email verification;
  trader business_name required; privilege-escalation (`role:admin`) blocked at registration.
- **Database (validated):** correct settings row per role; bcrypt hashes (never plaintext); audit log
  append-only entries for register/login/reset/verify.
- **Performance:** not yet measured (k6/Lighthouse pending).
- **Accessibility / UI / Responsive:** not yet measured (axe/Playwright pending).

### Files changed by the fixes (for the owner's review before deploy)

- `backend/alembic/versions/c4f1a9d3e7b2_fix_admin_enum_label_case.py` (new migration)
- `backend/app/schemas/auth.py` (email normalize on forgot/resend; reset policy parity; dedupe)
- `backend/requirements.txt` (`bcrypt==4.2.0` → `4.0.1`)
- `backend/tests/**` (new QA suite — does not ship in the app image)

> **Deploy note (owner-owned):** BUG-AUTH-001 is a DB migration. It auto-applies via your existing
> `alembic upgrade head` deploy step. The enum RENAME updates existing rows in place, so any
> operator-seeded admin survives. No data migration needed.

### Recommendations

1. **Apply the fixes** via your normal PR → CI → deploy flow (QA performed no git/CI/AWS actions).
2. **Build the UI E2E + a11y + perf layer** (next agreed step) before final auth sign-off.
3. **Monitor Redis availability** in prod (rate-limit fail-open).
4. Consider a brief throttle on `/forgot-password` itself (currently only register/login are throttled).

### Production-readiness score (Authentication, backend layer)

**8.5 / 10** — API/DB/security layer validated and green after fixes; deductions for (a) UI/a11y/perf
layer not yet executed, (b) rate-limit fail-open residual risk. Score revisits upward once the E2E
round passes.

### Next actions

- [ ] Owner: review + merge + deploy the 3 source changes.
- [ ] QA: build Sprint-1 E2E/a11y/perf suite under `/qa` (hybrid structure).
- [ ] Then: Sprint 1 fully exit-ready → STOP for approval → Sprint 2 (Landing, Contact).

---

*End of Sprint 1 Authentication QA report.*
