# Phase 3 — Module Test Design: Sprint 1 · Authentication

**Project:** Copy-Trading Platform (`D:\Workspace\copy-trading-app`)
**Module:** Authentication — Login, Register, Forgot Password, Reset Password, Verify Email
**Prepared:** 2026-06-29 · **Branch:** `qa-branch`
**Status:** DESIGN ONLY (no execution). Grounded in source read of:
`backend/app/api/auth.py`, `backend/app/schemas/auth.py`, `backend/app/core/security.py`,
`backend/app/services/rate_limit.py`, and the five frontend pages under `frontend/app/{login,register,forgot-password,reset-password,verify-email}`.

> Secrets redacted throughout. Execution targets the LOCAL stack only.

---

## A. Ground-truth reference (from code)

**Endpoints** (`/api/auth`):

| Endpoint                    | Body                                                 | Success                        | Errors                                                                       |
| --------------------------- | ---------------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------- |
| POST `/register`            | email, password, role, display_name?, business_name? | 201 `UserOut`                  | 409 `email_taken`, 422 validation, 429 `too_many_requests` (Retry-After 900) |
| POST `/login`               | email, password                                      | 200 `TokenPair`                | 401 `invalid_credentials`, 403 `user_inactive`, 429 `too_many_attempts`      |
| POST `/forgot-password`     | email                                                | 200 `MessageOut` (always same) | 422 bad email format                                                         |
| POST `/reset-password`      | token, new_password                                  | 200 `MessageOut`               | 400 `invalid_or_expired_token`, 422 (len)                                    |
| POST `/verify-email`        | token                                                | 200 `MessageOut` (idempotent)  | 400 `invalid_or_expired_token`                                               |
| POST `/resend-verification` | email                                                | 200 `MessageOut` (always same) | 422 bad email format                                                         |
| POST `/refresh`             | refresh_token                                        | 200 `TokenPair`                | 401 `invalid_token`/`wrong_token_type`/`user_inactive`                       |
| GET `/me`                   | (Bearer)                                             | 200 `UserOut`                  | 401                                                                          |

**Validation rules:**

- **Email:** `EmailStr`; register + login normalize via `strip().lower()` (before-validator). **forgot-password / resend-verification do NOT normalize** (only `EmailStr`, which lowercases the domain but not the local-part).
- **Register password:** 8–72 bytes AND ≥3 of {lowercase, uppercase, digit, symbol}.
- **Reset password:** `min_length=8, max_length=128`, **no complexity check, no 72-byte cap.**
- **role:** only `trader`/`subscriber` self-registrable; `admin` → 422 `role must be 'trader' or 'subscriber'`.
- **business_name:** required (non-empty after strip) for trader, max 120; forced `None` otherwise. `display_name` max 120.

**Tokens:** access 30 min (`sub`,`role`,`type=access`), refresh 14 d (`sub`,`type=refresh`), HS256.
Reset token `type=reset` + `pwf` = HMAC fingerprint of current password hash (→ single-use, 30-min TTL).
Verify token `type=verify` + `eml` = email at issue (24-h TTL).

**Rate limits (Redis, fail-OPEN if Redis down):** login lock ≥8 failed/email/15 min; login IP >40/15 min; register IP >15/hour. Retry-After = 900 s.

**Frontend behaviors:** login/register lowercase email per-keystroke + trim on submit; register auto-logs-in immediately after register; reset requires `?token=`; verify auto-calls on mount (StrictMode-guarded), busts `trading-app:user` cache.

---

## B. Candidate-defect watchlist (to VERIFY in Phase 5 — not yet asserted as bugs)

| #   | Area                     | Observation                                                                                                                                    | Linked tests                             |
| --- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| W1  | forgot-password / resend | Email not normalized (no `strip().lower()`). Mixed-case local-part won't match the lowercased stored email → reset/verify silently never sent. | AUTH-FORGOT-NEG-003, AUTH-VERIFY-NEG-004 |
| W2  | reset-password           | Weaker policy than register: no complexity rule, allows up to 128 chars (bcrypt truncates >72). A reset can set a weaker/truncated password.   | AUTH-RESET-BND-002, AUTH-RESET-SEC-003   |
| W3  | schemas/auth.py          | `ForgotPasswordIn`/`ResetPasswordIn`/`MessageOut` defined 3–4× (dead/duplicate code). Code smell; last def wins.                               | AUTH-REG-EXPL-002                        |
| W4  | landing redirect         | Login comment says trader→/trade-panel, subscriber→/trades; earlier map said /dashboard. Actual destination must be confirmed.                 | AUTH-LOGIN-FUNC-004                      |
| W5  | register→auto-login      | Register UI immediately calls /login; if that login is rate-limited/fails, user sees "registration failed" though the account WAS created.     | AUTH-REG-RECOV-001                       |
| W6  | rate-limit fail-open     | If Redis is down, brute-force protection is disabled by design — confirm documented behavior + risk.                                           | AUTH-LOGIN-SEC-004                       |

---

## C. Conventions

- **Test ID:** `AUTH-<FEATURE>-<TYPE>-<NNN>` (FEATURE: LOGIN/REG/FORGOT/RESET/VERIFY/X-cross-cutting).
- **Priority:** P0 (critical path) · P1 (high) · P2 (medium) · P3 (low).
- **Severity:** Critical / High / Medium / Low (impact if it fails).
- **Automation Candidate:** Yes-API / Yes-E2E / Yes-DB / Yes-a11y / Yes-perf / Manual.
- Layer tags: API = backend direct (httpx/pytest); E2E = Playwright UI; DB = SQL assertion.

---

## D. LOGIN (`/login`, POST `/api/auth/login`)

### Functional

| Field                   | Value                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **AUTH-LOGIN-FUNC-001** | Module: Auth · Feature: Login · **Scenario:** Valid trader credentials return a token pair · Priority: P0 · Severity: Critical |
| Preconditions           | Active verified trader exists                                                                                                  |
| Test Data               | email `qa.trader@example.test`, correct password                                                                               |
| Steps                   | POST /api/auth/login with valid body                                                                                           |
| Expected                | 200; body has `access_token`, `refresh_token`, `token_type="bearer"`; access JWT decodes with `role=trader`, `type=access`     |
| Post                    | Failed-attempt counter for email cleared; audit `user.login` row written                                                       |
| Automation              | Yes-API                                                                                                                        |

| **AUTH-LOGIN-FUNC-002** — Subscriber valid login. P0/Critical. Data: active subscriber. Expected: 200, `role=subscriber` in access token. Automation: Yes-API. |
| **AUTH-LOGIN-FUNC-003** — Admin valid login. P1/High. Data: seeded admin. Expected: 200, `role=admin`. Automation: Yes-API. |
| **AUTH-LOGIN-FUNC-004** — UI login routes to role landing (verifies **W4**). P0/High. Steps: log in as trader via UI → observe destination; repeat subscriber. Expected: deterministic role-correct landing (record actual path). Post: tokens in localStorage. Automation: Yes-E2E. |
| **AUTH-LOGIN-FUNC-005** — Case-insensitive email: login with `QA.Trader@Example.test`. P1/High. Expected: 200 (normalized to lowercase matches stored). Automation: Yes-API. |
| **AUTH-LOGIN-FUNC-006** — Whitespace-padded email `"  qa.trader@example.test  "`. P2/Medium. Expected: 200 (trimmed). Automation: Yes-API. |

### Business Rule

| **AUTH-LOGIN-BIZ-001** — Unverified email still logs in (soft enforcement). P1/High. Data: unverified user. Expected: 200 token pair (no block). Automation: Yes-API. |
| **AUTH-LOGIN-BIZ-002** — Inactive user blocked. P0/Critical. Data: `is_active=false`. Expected: 403 `user_inactive` (after password verified). Automation: Yes-API + DB setup. |

### Negative

| **AUTH-LOGIN-NEG-001** — Wrong password. P0/Critical. Expected: 401 `invalid_credentials`; failure counter incremented; audit `user.login_failed`. Automation: Yes-API. |
| **AUTH-LOGIN-NEG-002** — Unknown email. P1/High. Expected: 401 `invalid_credentials` (same as wrong pw — no user enumeration / no timing oracle expectation documented). Automation: Yes-API. |
| **AUTH-LOGIN-NEG-003** — Missing password field. P2/Medium. Expected: 422. Automation: Yes-API. |
| **AUTH-LOGIN-NEG-004** — Malformed email `not-an-email`. P2/Medium. Expected: 422. Automation: Yes-API. |
| **AUTH-LOGIN-NEG-005** — Empty body `{}`. P2/Medium. Expected: 422. Automation: Yes-API. |

### Boundary

| **AUTH-LOGIN-BND-001** — Failed-login lockout threshold: 8th failure still 401, **9th** attempt returns 429 within 15-min window. P0/Critical. Data: one email, repeated wrong pw. Expected: lock at count ≥8; 429 `too_many_attempts` + `Retry-After: 900`; audit `user.login_rate_limited`. Post: counter TTL 900s. Automation: Yes-API. |
| **AUTH-LOGIN-BND-002** — Per-IP throttle: >40 attempts/15 min from one IP → 429. P1/High. Expected: 429 after 40. Automation: Yes-API (XFF control). |
| **AUTH-LOGIN-BND-003** — Successful login mid-window resets the email failure counter (7 fails then success then 8 more should not insta-lock). P1/High. Automation: Yes-API. |

### Validation / UI / UX

| **AUTH-LOGIN-VAL-001** — Email input lowercases on keystroke (`User@` → `user@`). P2/Medium. Automation: Yes-E2E. |
| **AUTH-LOGIN-UI-001** — Renders: email, password (masked), show/hide toggle, "Forgot password?" link, "Create an account" link, Sign-in button. P2/Medium. Automation: Yes-E2E. |
| **AUTH-LOGIN-UI-002** — Submit shows spinner + disables button while in-flight. P2/Low. Automation: Yes-E2E. |
| **AUTH-LOGIN-UX-001** — Failed login surfaces a toast (no silent failure); form remains filled (email retained). P2/Medium. Automation: Yes-E2E. |
| **AUTH-LOGIN-UX-002** — PasswordInput show/hide toggles masking. P3/Low. Automation: Yes-E2E. |

### Session

| **AUTH-LOGIN-SESS-001** — Already-authenticated visitor hitting `/login` is redirected to `/`. P1/High. Pre: valid token in localStorage. Automation: Yes-E2E. |
| **AUTH-LOGIN-SESS-002** — Tokens persisted to localStorage keys `trading-app:access`/`:refresh` after login. P1/High. Automation: Yes-E2E. |
| **AUTH-LOGIN-SESS-003** — Refresh flow: expired access + valid refresh → silent refresh yields new pair; original request retried. P0/Critical. Automation: Yes-API (POST /refresh) + Yes-E2E. |

### Authorization

| **AUTH-LOGIN-AUTHZ-001** — `/api/auth/me` with no token → 401. P1/High. Automation: Yes-API. |
| **AUTH-LOGIN-AUTHZ-002** — `/api/auth/me` with valid access token returns the caller's own UserOut (role/email correct). P1/High. Automation: Yes-API. |

### Security

| **AUTH-LOGIN-SEC-001** — JWT tampering: alter payload role to `admin`, keep old signature → rejected (signature check). P0/Critical. Automation: Yes-API. |
| **AUTH-LOGIN-SEC-002** — `alg:none` / unsigned token rejected. P0/Critical. Automation: Yes-API. |
| **AUTH-LOGIN-SEC-003** — Expired access token rejected on `/me` (forge exp in past). P1/High. Automation: Yes-API. |
| **AUTH-LOGIN-SEC-004** — Rate-limit fail-open (verifies **W6**): with Redis stopped, login still functions (no lockout). Document the security trade-off. P1/High. Pre: stop Redis container. Automation: Manual/Yes-API (controlled). |
| **AUTH-LOGIN-SEC-005** — SQLi attempt in email/password (`' OR 1=1 --`) → 401/422, no auth bypass, no error leak. P0/Critical. Automation: Yes-API. |
| **AUTH-LOGIN-SEC-006** — Account-lockout does not leak whether the email exists (locked response identical for real vs fake email). P2/Medium. Automation: Yes-API. |

### Performance

| **AUTH-LOGIN-PERF-001** — Login API p95 < 500 ms under 20 VUs (paper data). P2/Medium. Tool: k6. Automation: Yes-perf. |

### Recovery

| **AUTH-LOGIN-RECOV-001** — Backend returns 5xx/timeout mid-login → UI shows error toast, no token stored, retry works. P2/Medium. Automation: Yes-E2E (mock). |

### Smoke / Sanity / Regression

| **AUTH-LOGIN-SMOKE-001** — Each role can log in and reach `/me`. P0/Critical. Automation: Yes-API. (Part of module smoke.) |
| **AUTH-LOGIN-SANITY-001** — Post-fix: valid login + wrong-password + lockout still behave. P1. Automation: Yes-API. |
| **AUTH-LOGIN-RGRS-001** — Login suite re-runs green against baseline after any auth change. P1. Automation: Yes-API. |

### Exploratory

| **AUTH-LOGIN-EXPL-001** — Charter: probe login with unicode/emoji emails, very long passwords (72-byte bcrypt boundary), concurrent logins, paste-with-newline. Time-box 45 min; log anomalies. P2. Automation: Manual. |

---

## E. REGISTER (`/register`, POST `/api/auth/register`)

### Functional

| **AUTH-REG-FUNC-001** — Register subscriber (happy path). P0/Critical. Data: unique email, valid password `Str0ng!pw`, role subscriber. Expected: 201 UserOut (`role=subscriber`, `email_verified=false`); `SubscriberSettings` row created (`copy_enabled=false`, `multiplier=1.000`); audit `user.register`; verification email queued. Post: user persisted. Automation: Yes-API + Yes-DB. |
| **AUTH-REG-FUNC-002** — Register trader with business_name. P0/Critical. Data: role trader, business_name "QA Capital". Expected: 201; `TraderSettings` row (`trading_enabled=true`); business_name stored trimmed. Automation: Yes-API + Yes-DB. |
| **AUTH-REG-FUNC-003** — Multi-trader allowed (verifies README is stale): register a 2nd trader. P1/High. Expected: 201 (no one-trader rejection). Automation: Yes-API. |
| **AUTH-REG-FUNC-004** — UI register → auto-login → lands in app with success toast. P1/High. Automation: Yes-E2E. |
| **AUTH-REG-FUNC-005** — display_name optional: omit it → 201, `display_name=null`. P2/Medium. Automation: Yes-API. |

### Business Rule

| **AUTH-REG-BIZ-001** — Trader without business_name rejected. P0/Critical. Expected: 422 `business_name is required for traders`. Automation: Yes-API. |
| **AUTH-REG-BIZ-002** — Subscriber with business_name → stored as `null` (forced None). P2/Medium. Automation: Yes-API + DB. |
| **AUTH-REG-BIZ-003** — Verification email is sent but account is immediately usable (soft verify). P1/High. Automation: Yes-API (assert email-service called / log) + DB. |

### Negative / Security (privilege)

| **AUTH-REG-NEG-001** — Duplicate email → 409 `email_taken`. P0/Critical. Pre: email already registered. Automation: Yes-API. |
| **AUTH-REG-NEG-002** — Duplicate with different case (`QA@x` vs `qa@x`) still 409 (normalization). P1/High. Automation: Yes-API. |
| **AUTH-REG-SEC-001** — **Privilege escalation:** `role:"admin"` → 422, no admin created. P0/Critical. Automation: Yes-API + DB (assert no admin row). |
| **AUTH-REG-SEC-002** — Unknown role `"superuser"` → 422. P1/High. Automation: Yes-API. |
| **AUTH-REG-SEC-003** — XSS payload in business_name/display_name (`<script>`) stored & later rendered escaped (no execution in shell wordmark). P1/High. Automation: Yes-E2E + DB. |
| **AUTH-REG-NEG-003** — Missing required password → 422. P2/Medium. Automation: Yes-API. |

### Boundary / Validation (password policy)

| **AUTH-REG-BND-001** — Password exactly 8 chars meeting 3 classes (`Ab1!xxxx`) → 201. P1/High. Automation: Yes-API. |
| **AUTH-REG-BND-002** — Password 7 chars → 422 (min length). P1/High. Automation: Yes-API. |
| **AUTH-REG-BND-003** — Password 8 chars, only lowercase (`aaaaaaaa`) → 422 (needs ≥3 classes). P0/Critical (passes client minLength=8 but server must reject). Automation: Yes-API + Yes-E2E. |
| **AUTH-REG-BND-004** — Password 73 bytes → 422 (bcrypt 72-byte cap). P1/High. Data: 73 ASCII chars. Automation: Yes-API. |
| **AUTH-REG-BND-005** — Multibyte password near 72-byte boundary (e.g., emoji) → correct accept/reject at byte boundary, not char count. P2/Medium. Automation: Yes-API. |
| **AUTH-REG-VAL-001** — business_name 121 chars → 422 (max 120). P2/Medium. Automation: Yes-API. |
| **AUTH-REG-VAL-002** — display_name 121 chars → 422. P3/Low. Automation: Yes-API. |
| **AUTH-REG-VAL-003** — Malformed email → 422. P2/Medium. Automation: Yes-API. |
| **AUTH-REG-VAL-004** — Email normalized + stored lowercase/trimmed. P1/High. Automation: Yes-API + DB. |

### Rate limit

| **AUTH-REG-BND-006** — >15 registrations/hour from one IP → 429 `too_many_requests` + Retry-After. P1/High. Automation: Yes-API (XFF). |

### Database

| **AUTH-REG-DB-001** — On register, exactly one `users` row + exactly one settings row of the right kind; no orphan settings. P1/High. Automation: Yes-DB. |
| **AUTH-REG-DB-002** — `password_hash` is bcrypt (never plaintext); `audit_logs` has append-only `user.register`. P0/Critical. Automation: Yes-DB. |

### UI / UX / Validation (client)

| **AUTH-REG-UI-001** — Role toggle (Subscriber/Trader); business_name field appears only for trader, marked required `*`. P2/Medium. Automation: Yes-E2E. |
| **AUTH-REG-UX-001** — Submitting trader with blank business_name shows client toast before any request. P2/Medium. Automation: Yes-E2E. |
| **AUTH-REG-UX-002** — Password field has `autoComplete="new-password"`, placeholder "8+ characters". P3/Low. Automation: Yes-E2E. |
| **AUTH-REG-VAL-005** — Client minLength=8 blocks submit of short password (HTML validation) before request. P2/Medium. Automation: Yes-E2E. |

### Recovery

| **AUTH-REG-RECOV-001** — (verifies **W5**) Register succeeds but the immediate auto-login is rate-limited/fails → assess UX (account created yet error shown). P2/Medium. Automation: Yes-E2E (mock 429 on 2nd call). |

### Smoke / Sanity / Regression / Exploratory

| **AUTH-REG-SMOKE-001** — Register one subscriber + one trader succeeds. P0. Automation: Yes-API. |
| **AUTH-REG-SANITY-001** — Post-fix: happy register + duplicate + weak-password still behave. P1. Automation: Yes-API. |
| **AUTH-REG-RGRS-001** — Register suite green vs baseline. P1. Automation: Yes-API. |
| **AUTH-REG-EXPL-002** — Charter (verifies **W3**): fuzz body (extra fields, null role, array email, mass-assignment of `is_active`/`email_verified`). Confirm server ignores non-schema fields. P1. Automation: Manual + Yes-API. |

---

## F. FORGOT PASSWORD (`/forgot-password`, POST `/api/auth/forgot-password`)

### Functional / Business Rule

| **AUTH-FORGOT-FUNC-001** — Existing active user requests reset → 200 generic message; reset email queued; audit `user.password_reset_requested`. P0/Critical. Automation: Yes-API + DB. |
| **AUTH-FORGOT-BIZ-001** — Non-existent email → 200 identical message, **no** email sent, **no** audit row (anti-enumeration). P0/Critical. Automation: Yes-API + DB. |
| **AUTH-FORGOT-BIZ-002** — Inactive user → 200 generic message, no token minted/email sent. P1/High. Automation: Yes-API. |
| **AUTH-FORGOT-FUNC-002** — UI shows the "if an account exists…" confirmation screen after submit (regardless of existence). P1/High. Automation: Yes-E2E. |

### Negative / Security

| **AUTH-FORGOT-NEG-001** — Malformed email → 422. P2/Medium. Automation: Yes-API. |
| **AUTH-FORGOT-NEG-002** — Missing email field → 422. P3/Low. Automation: Yes-API. |
| **AUTH-FORGOT-NEG-003** — (verifies **W1**) Mixed-case local-part `QA.Trader@example.test` for a user stored as `qa.trader@…`: confirm whether a reset is actually sent. Expected (correct): treated case-insensitively and sent. **Likely actual:** no match → no email (response still 200). P1/High. Automation: Yes-API + DB (assert audit row presence). |
| **AUTH-FORGOT-SEC-001** — Response body + status + timing do not differentiate existing vs non-existing email (enumeration resistance). P1/High. Automation: Yes-API. |
| **AUTH-FORGOT-SEC-002** — Endpoint reachable without auth (by design) but throttled if abused (note: no dedicated limiter on forgot — flag as observation). P2/Medium. Automation: Yes-API. |

### UI / Smoke / Regression / Exploratory

| **AUTH-FORGOT-UI-001** — Email field + "Send reset link" + "Sign in" link; spinner on submit. P3/Low. Automation: Yes-E2E. |
| **AUTH-FORGOT-SMOKE-001** — Submit for known user returns 200. P1. Automation: Yes-API. |
| **AUTH-FORGOT-RGRS-001** — Suite green vs baseline. P2. Automation: Yes-API. |
| **AUTH-FORGOT-EXPL-001** — Charter: repeated requests (token churn), header injection in email, very long local-part. P2. Automation: Manual. |

---

## G. RESET PASSWORD (`/reset-password`, POST `/api/auth/reset-password`)

### Functional / Business Rule

| **AUTH-RESET-FUNC-001** — Valid fresh token + strong new password → 200; password hash changes; user can log in with the new password and NOT the old. P0/Critical. Pre: mint reset token for user. Automation: Yes-API + DB. |
| **AUTH-RESET-BIZ-001** — **Single-use:** reuse the same token after a successful reset → 400 `invalid_or_expired_token` (fingerprint no longer matches). P0/Critical. Automation: Yes-API. |
| **AUTH-RESET-BIZ-002** — Outstanding token invalidated when password changes via another route. P1/High. Automation: Yes-API. |
| **AUTH-RESET-FUNC-002** — UI: valid `?token` → form; success → redirect `/login` with toast. P1/High. Automation: Yes-E2E. |

### Negative

| **AUTH-RESET-NEG-001** — Tampered/garbage token → 400. P0/Critical. Automation: Yes-API. |
| **AUTH-RESET-NEG-002** — Expired reset token (>30 min) → 400. P1/High. Pre: mint with backdated exp (or config). Automation: Yes-API. |
| **AUTH-RESET-NEG-003** — Wrong token type (pass an access token) → 400. P1/High. Automation: Yes-API. |
| **AUTH-RESET-NEG-004** — Token for an inactive/deleted user → 400. P1/High. Automation: Yes-API. |
| **AUTH-RESET-NEG-005** — Missing token field → 422. P3/Low. Automation: Yes-API. |
| **AUTH-RESET-UI-001** — No `?token` in URL → "invalid or incomplete" screen + "Request a new link". P1/High. Automation: Yes-E2E. |

### Boundary / Validation / Security (policy gap)

| **AUTH-RESET-BND-001** — new_password 7 chars → 422 (min 8). P1/High. Automation: Yes-API. |
| **AUTH-RESET-BND-002** — (verifies **W2**) new_password 100 chars (>72 bytes) accepted by API (max 128) — confirm bcrypt-truncation behavior and that only first 72 bytes authenticate. P1/High. Automation: Yes-API. |
| **AUTH-RESET-SEC-003** — (verifies **W2**) Reset to an all-lowercase 8-char password (`aaaaaaaa`) **succeeds** (no complexity rule) whereas register would reject it → policy inconsistency. P1/High. Automation: Yes-API. |
| **AUTH-RESET-SEC-001** — Reset token not logged/leaked in server logs or error bodies. P2/Medium. Automation: Manual/log scan. |
| **AUTH-RESET-VAL-001** — UI: new ≠ confirm → client toast "Passwords don't match", no request. P2/Medium. Automation: Yes-E2E. |

### Database / Recovery / Smoke / Regression / Exploratory

| **AUTH-RESET-DB-001** — After reset, `users.password_hash` differs from prior; audit `user.password_reset` appended. P1/High. Automation: Yes-DB. |
| **AUTH-RESET-RECOV-001** — Backend 5xx mid-reset → error toast, no redirect, retry possible. P3/Low. Automation: Yes-E2E (mock). |
| **AUTH-RESET-SMOKE-001** — End-to-end: forgot → (capture token from log/email mock) → reset → login with new pw. P0/Critical. Automation: Yes-API. |
| **AUTH-RESET-RGRS-001** — Suite green vs baseline. P1. Automation: Yes-API. |
| **AUTH-RESET-EXPL-001** — Charter: token from another user, swapped tokens, replay after partial failure. P2. Automation: Manual. |

---

## H. VERIFY EMAIL (`/verify-email`, POST `/api/auth/verify-email`)

### Functional / Business Rule

| **AUTH-VERIFY-FUNC-001** — Valid token → 200; `email_verified=true`, `email_verified_at` set; audit `user.email_verified`. P0/Critical. Automation: Yes-API + DB. |
| **AUTH-VERIFY-BIZ-001** — **Idempotent:** verifying an already-verified account → 200, no duplicate audit / no timestamp overwrite. P1/High. Automation: Yes-API + DB. |
| **AUTH-VERIFY-FUNC-002** — UI auto-verifies on mount; shows success state + "Continue"; busts `trading-app:user` cache so banner clears. P1/High. Automation: Yes-E2E. |
| **AUTH-VERIFY-BIZ-002** — Resend-verification: existing unverified active user → new token emailed; already-verified user → 200 but no email. P1/High. Automation: Yes-API. |

### Negative / Security

| **AUTH-VERIFY-NEG-001** — Garbage/tampered token → 400; UI shows error state. P0/Critical. Automation: Yes-API + Yes-E2E. |
| **AUTH-VERIFY-NEG-002** — Expired token (>24 h) → 400. P1/High. Automation: Yes-API. |
| **AUTH-VERIFY-NEG-003** — Wrong token type (access/reset token) → 400. P1/High. Automation: Yes-API. |
| **AUTH-VERIFY-NEG-004** — Stale `eml`: token issued for old email after the email changed → 400 (`eml` mismatch). Also exercises **W1** path. P1/High. Automation: Yes-API. |
| **AUTH-VERIFY-NEG-005** — No `?token` in URL → UI error state immediately (no request). P2/Medium. Automation: Yes-E2E. |
| **AUTH-VERIFY-SEC-001** — Cannot verify another user's email by swapping `sub`/`eml` (signature binds them). P1/High. Automation: Yes-API. |

### UI / Smoke / Regression / Exploratory

| **AUTH-VERIFY-UI-001** — Three visual states render correctly: verifying (spinner) / success (check) / error (warning); "Continue" target depends on logged-in. P2/Medium. Automation: Yes-E2E. |
| **AUTH-VERIFY-UI-002** — StrictMode double-invoke guard: token verified once (no double POST). P2/Medium. Automation: Yes-E2E (network count). |
| **AUTH-VERIFY-SMOKE-001** — Register → capture verify token → verify → `email_verified=true`. P1. Automation: Yes-API. |
| **AUTH-VERIFY-RGRS-001** — Suite green vs baseline. P2. Automation: Yes-API. |
| **AUTH-VERIFY-EXPL-001** — Charter: re-use token after email change, concurrent verifies. P3. Automation: Manual. |

---

## I. Cross-cutting (apply across all 5 pages)

### Accessibility (axe-core)

| **AUTH-X-A11Y-001** — Zero serious/critical axe violations on /login, /register, /forgot-password, /reset-password, /verify-email. P1/High. Automation: Yes-a11y. |
| **AUTH-X-A11Y-002** — All inputs have associated labels; password show/hide toggle is keyboard-operable + has accessible name. P2/Medium. Automation: Yes-a11y/E2E. |
| **AUTH-X-A11Y-003** — Full keyboard-only flow (tab order, focus ring, Enter submits). P2/Medium. Automation: Manual + E2E. |
| **AUTH-X-A11Y-004** — Color contrast of muted/label/button text meets WCAG AA in light AND dark theme. P2/Medium. Automation: Yes-a11y. |

### Responsive

| **AUTH-X-RESP-001** — Auth cards render correctly at 320 / 375 / 768 / 1024 / 1440 px (no overflow, tappable targets ≥40px). P2/Medium. Automation: Yes-E2E (Playwright viewports). |

### Cross-browser

| **AUTH-X-XBROWSER-001** — Login + register journeys pass on Chromium, Firefox, WebKit. P2/Medium. Automation: Yes-E2E (Playwright projects). |

### Theme / UX

| **AUTH-X-UX-001** — Light/dark theme toggle persists and applies to auth pages. P3/Low. Automation: Yes-E2E. |

### Performance

| **AUTH-X-PERF-001** — Lighthouse on /login: LCP < 2.5 s, no blocking errors (local). P3/Low. Automation: Yes-perf. |

### Security (transport / headers)

| **AUTH-X-SEC-001** — CORS: only configured origin (`http://localhost:3000`) is allowed with credentials; other origins rejected. P1/High. Automation: Yes-API. |
| **AUTH-X-SEC-002** — No secret/token values appear in server logs for any auth flow. P2/Medium. Automation: Manual/log scan. |
| **AUTH-X-SEC-003** — Error responses are generic (no stack traces / SQL in body) on 4xx/5xx. P1/High. Automation: Yes-API. |

---

## J. Traceability & coverage summary

| Feature   | Func | Biz | Neg | Bnd | Val | UI/UX | Sess | Authz | Sec | DB  | Perf | Recov | Smoke | Sanity | Rgrs | Expl | A11y/Resp/XB |
| --------- | ---- | --- | --- | --- | --- | ----- | ---- | ----- | --- | --- | ---- | ----- | ----- | ------ | ---- | ---- | ------------ |
| Login     | 6    | 2   | 5   | 3   | 1   | 4     | 3    | 2     | 6   | —   | 1    | 1     | 1     | 1      | 1    | 1    | via X        |
| Register  | 5    | 3   | 3   | 6   | 5   | 4     | —    | —     | 3   | 2   | —    | 1     | 1     | 1      | 1    | 1    | via X        |
| Forgot    | 2    | 2   | 3   | —   | —   | 1     | —    | —     | 2   | —   | —    | —     | 1     | —      | 1    | 1    | via X        |
| Reset     | 2    | 2   | 5   | 2   | 1   | 1     | —    | —     | 2   | 1   | —    | 1     | 1     | —      | 1    | 1    | via X        |
| Verify    | 2    | 2   | 5   | —   | —   | 2     | —    | —     | 1   | —   | —    | —     | 1     | —      | 1    | 1    | via X        |
| Cross-cut | —    | —   | —   | —   | —   | 1     | —    | —     | 3   | —   | 1    | —     | —     | —      | —    | —    | 6            |

**Approx. total designed cases: ~110** across all 21 required test types.
**Risk coverage:** R4 (authz), R6 (auth/session), R11 (secrets) directly exercised this sprint; W1–W6 each have an assigned verification case.

---

## K. Open questions for sign-off

1. **W4 landing path** — confirm intended trader/subscriber post-login destination (so AUTH-LOGIN-FUNC-004 has a hard expected value).
2. **Email-capture for reset/verify tokens** in local testing — OK to read tokens from backend logs / a SendGrid sandbox / DB, or should I add a local mail-catcher? (Needed for AUTH-RESET-SMOKE-001 / AUTH-VERIFY-SMOKE-001.)
3. **W1/W2 expected behavior** — should I treat the forgot-password case-normalization gap and the reset-password weaker policy as **defects to file** in Phase 6, or accepted-as-is? (Affects expected results.)
4. Scope check: include `/refresh` and `/me` fully in this sprint (they're auth-adjacent) — assumed **yes**.

---

_End of Phase 3 — Authentication test design._
