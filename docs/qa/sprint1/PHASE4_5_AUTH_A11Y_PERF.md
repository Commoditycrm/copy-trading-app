# Sprint 1 · Authentication — Accessibility + Performance Layer

**Date:** 2026-06-29 · **Tools:** axe-core (WCAG 2.0/2.1 A+AA) via Playwright · Playwright navigation-timing perf smoke
**Suite:** `qa/e2e/a11y/`, `qa/e2e/perf/` · **Environment:** LOCAL (frontend :3000 dev server)

---

## Accessibility (axe-core)

**4 / 5 pages pass** for serious+critical. One real defect found.

| Page | Serious/Critical | Result |
|---|---|---|
| /login | 0 | ✅ pass* |
| /register | **1 critical** (`label`) | ❌ **BUG-A11Y-001** |
| /forgot-password | 0 | ✅ pass* |
| /reset-password (no token) | 0 | ✅ pass |
| /verify-email (no token) | 0 | ✅ pass |

\* Pass is *fragile*: login/forgot inputs satisfy axe only via their `placeholder`, which is a weak,
disappearing label (poor for screen-reader + zoom users). See BUG-A11Y-001 recommendation.

### BUG-A11Y-001 — auth form inputs lack programmatically-associated labels
- **Status:** ✅ FIXED on branch `fix/auth-a11y-labels` (commit 9bdb774). Re-run: **a11y 5/5 pass.**
- **Severity:** Medium (register instance is axe **critical**).
- **What:** The auth pages render visible `<label>` text but do **not** associate it with the input
  (no `htmlFor`/`id`, no `aria-label`, no `aria-labelledby`). The `/register` **Display Name** input
  has *no* placeholder either, so it has **no accessible name at all** → axe critical
  `label` ("Form elements must have labels"). The other inputs escape a hard failure only because a
  `placeholder` stands in for the name — an anti-pattern (vanishes on typing; not a reliable label).
- **Impact:** Screen-reader users can't reliably tell what the Display Name field is; placeholder-only
  labelling is a known WCAG 1.3.1 / 3.3.2 weakness across the auth forms.
- **Affected:** `frontend/app/register/page.tsx` (Display Name + others), and by pattern
  `login`, `forgot-password`, `reset-password` inputs.
- **Fix (minimal):** give each `<label>` a `htmlFor` and its input a matching `id` (or add
  `aria-label` to each input). Small, localized, no behavior change.

---

## Performance (local dev-server smoke)

**2 / 2 pass.** Measured on the *warm/compiled* route (dev mode; not a production Lighthouse audit).

| Route | TTFB | DOMContentLoaded | Load | LCP | Budget (LCP<5s, load<7s) |
|---|---|---|---|---|---|
| /login | 24 ms | 42 ms | 301 ms | 828 ms | ✅ |
| /register | 28 ms | 46 ms | 292 ms | 812 ms | ✅ |

Auth pages are lightweight and fast locally. **Note:** this is a Playwright navigation-timing smoke,
not a full Lighthouse/k6 run — production-build Lighthouse + k6 load are deferred to Sprint 8
(cross-cutting performance), where a running prod build is the right target.

---

## Recap of E2E-layer finding

- **OBS-E2E-001 (Low):** ✅ FIXED on `fix/auth-a11y-labels` — removed the unreachable JS
  `notify.error(...)` guard (native `required` + server validator already enforce it).

---

## Sprint-1 auth coverage now

| Layer | Status |
|---|---|
| API / DB / Security (pytest) | ✅ 77/77 (on `qa/sprint1-auth-fixes`) |
| E2E UI (Playwright) | ✅ 17/17 |
| Accessibility (axe) | ⚠️ 4/5 — BUG-A11Y-001 open |
| Performance (local smoke) | ✅ 2/2 |
| Cross-browser (Firefox/WebKit), full Lighthouse/k6 | ⏳ Sprint 8 |

*Run:* `cd qa && npx playwright test` (whole suite) — stack must be up (see `qa/README.md`).
