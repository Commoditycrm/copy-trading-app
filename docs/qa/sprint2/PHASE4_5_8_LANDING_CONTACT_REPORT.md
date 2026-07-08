# Sprint 2 · Landing + Contact — Automation + Execution + Report

**Date:** 2026-06-29 · **Module:** Landing (root `/`) + Contact (`/contact`)
**Layers:** E2E (Playwright/Chromium) · Accessibility (axe) · Performance (local smoke)
**Suite:** `qa/e2e/landing.spec.ts`, `qa/e2e/contact/`, `qa/e2e/a11y/contact.a11y.spec.ts`, `qa/e2e/perf/contact.perf.spec.ts`
**Environment:** LOCAL — frontend :3000, backend :8000 (qa-branch code, e2e DB). Production untouched.

---

## Result

**19 passed · 1 skipped · 0 failed.** No functional defects found.

| Feature | Tests | Pass | Skip | Notes |
|---|---|---|---|---|
| Landing (root redirect) | 6 | 5 | 1 | admin-landing skipped (blocked, see below) |
| Contact | 8 | 8 | 0 | incl. XSS-safety + no-network mailto path |
| Contact a11y (axe) | 1 | 1 | 0 | 0 serious/critical (labels associated via `<Field htmlFor>`) |
| Contact perf | 1 | 1 | 0 | TTFB 12ms · load 206ms · LCP 56ms |
| Sprint-1 login regression | 4 | 4 | 0 | re-verified after `seedTokens` refactor |

### Blocked
- **LAND-FUNC-004** (admin → `/admin`) — **skipped**, blocked by **BUG-AUTH-001** (admin `user_role`
  enum case) which is not deployed on the running qa-branch backend, so an admin user can't be
  created/read. Covered once that fix lands. (Fix exists on `qa/sprint1-auth-fixes`.)

---

## Findings

**No functional bugs.** The four design-time observations are confirmed as behaviors (not defects) —
UX/consistency calls for the product owner:

| # | Confirmed behavior | Type |
|---|---|---|
| OBS-LAND-001 | No marketing home; `/` redirects. Anonymous visitors (and Contact's "Home"/"Back to home" links) land on `/login`. | By-design / confirm intent |
| OBS-CONTACT-001 | mailto path shows **"sent"** immediately after opening the mail client — nothing is actually delivered; optimistic confirmation. | UX (optional copy change) |
| OBS-CONTACT-002 | Public brand wordmark is **"ARK"**, while transactional emails brand as **"Kopyya"**. | Consistency |
| OBS-CONTACT-003 | Field values enter the `mailto:` via `encodeURIComponent` (escapes CRLF). XSS test (CONTACT-SEC-001) **passed** — payloads never reach the DOM. Header-injection (CONTACT-SEC-002) is **code-verified** (encoding), not fully intercepted in E2E (setting `window.location.href` can't be cleanly spied); low residual risk. | Security (verified) |

---

## Coverage notes

Automated the E2E/a11y/perf-suitable cases. Deferred to Sprint 8 cross-cutting (or manual): keyboard
a11y walk (CONTACT-A11Y-002), responsive viewport matrix (CONTACT-RESP-001), cross-browser
(Firefox/WebKit), and the `FORM_ENDPOINT`-set Formspree path (not the shipped config). Landing's
`/me`-network-error recovery (LAND-RECOV-001) needs request mocking — deferred.

---

## Production-readiness (Landing + Contact)

**9 / 10.** Both are simple, fast, accessible public surfaces with correct redirect logic and safe
form handling. Deductions: admin-landing not verifiable on this branch (BUG-AUTH-001), plus two
minor UX/consistency observations (optimistic "sent", ARK vs Kopyya brand).

## Recommendations
1. Optional: soften Contact's "sent" copy to "Opening your mail app…" (OBS-CONTACT-001).
2. Optional: align public brand wordmark with the email brand (OBS-CONTACT-002).
3. Re-run LAND-FUNC-004 once BUG-AUTH-001 is deployed.

*Run:* `cd qa && npx playwright test e2e/landing.spec.ts e2e/contact` (stack up — see `qa/README.md`).
