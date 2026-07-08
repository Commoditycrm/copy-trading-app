# Sprint 1 · Authentication — E2E (UI) Layer

**Date:** 2026-06-29 · **Layer:** End-to-end UI (Playwright, Chromium)
**Suite:** `qa/e2e/auth/` (self-contained, hybrid `/qa` area)
**Environment:** LOCAL — frontend :3000, backend :8000 (e2e DB `trading_app_e2e`, Redis db 2, SendGrid disabled → links logged)

---

## Result

**17 / 17 passed** (31 s, Chromium). Full end-to-end flows exercised through the real browser →
Next proxy → backend → Postgres path, including token-driven reset and verify captured from the
backend email log.

| Feature | Tests | Pass |
|---|---|---|
| Login | 4 (render, invalid→toast, valid→/dashboard, already-authed redirect) | 4 |
| Register | 4 (happy→app, trader business-name guard, weak-pw server reject, duplicate) | 4 |
| Forgot Password | 3 (render, known→confirmation, unknown→same confirmation) | 3 |
| Reset Password | 3 (no-token screen, mismatch toast, **full reset→login with new pw**) | 3 |
| Verify Email | 3 (no-token error, invalid-token error, **full verify success**) | 3 |

Confirmed at UI level: role-based landing (`/dashboard`), anti-enumeration on forgot-password,
single-use reset (old password rejected after reset), StrictMode-safe verify.

---

## Findings

| ID | Severity | Finding |
|---|---|---|
| OBS-E2E-001 | Low | Register page: a trader with an empty business name is blocked by the input's native `required` attribute, which fires **before** the JS `notify.error("Business name is required for traders")`. That custom toast is therefore unreachable dead code. Functional guard is intact; only the tailored message never shows. Suggest removing the dead branch or dropping `required` so the JS message can render. |

No functional defects found in the auth UI flows.

---

## How token-dependent flows are tested without email

The backend runs with `SENDGRID_API_KEY` blank, so `email.py` **logs** the reset/verify link
(`reset_link=…` / `verify_link=…`) instead of sending. The suite's `waitForEmailLink()` polls the
backend log, matches the `to=<email>` block, and extracts the token — giving a real, end-to-end
reset/verify without a mail server.

---

## Still pending for full Sprint-1 sign-off

- **Accessibility** (axe-core) across the 5 auth pages
- **Responsive** viewports + **cross-browser** (Firefox/WebKit projects)
- **Performance** (Lighthouse on /login)

*Run:* `cd qa && npm run test:auth` (stack must be up — see `qa/README.md`).
