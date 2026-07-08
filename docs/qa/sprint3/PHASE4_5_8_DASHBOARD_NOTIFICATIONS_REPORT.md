# Sprint 3 · Trader Dashboard + Notifications — Automation + Execution + Report

**Date:** 2026-06-29 · **Module:** Trader Dashboard (`/dashboard`) + Notifications
**Layers:** API/DB (pg-backed arrange/assert) · E2E (Playwright/Chromium) · a11y (axe) · perf (local smoke)
**Suite:** `qa/e2e/dashboard.spec.ts`, `qa/e2e/notifications/`, `qa/e2e/a11y/`, `qa/e2e/perf/dashboard.perf.spec.ts`, `qa/e2e/db.ts`
**Environment:** LOCAL — frontend :3000, backend :8000 (qa-branch, `RUN_BACKGROUND_WORKERS=false`), e2e DB `trading_app_e2e`. Production untouched.

---

## Result

**21 passed · 0 failed.** No functional defects.

| Feature | Tests | Pass | Notes |
|---|---|---|---|
| Notifications · API | 8 | 8 | list scoping, unread-count, mark-read idempotent, read-all, **IDOR (404)**, 401/404 |
| Notifications · UI | 5 | 5 | empty state, newest-first + mark-read→DB, Order-History link, bell badge + mark-all, Escape close |
| Dashboard | 5 | 5 | loads (trader), zero-brokers ($0/"Across 0"), **equity = Σ seeded balances ($3,500)**, **active subs "2/3 copying"**, graceful degrade on `/positions` 500 |
| a11y | 2 | 2 | `/dashboard` (Recharts SVG) + `/notifications` — 0 serious/critical |
| Perf | 1 | 1 | `/dashboard` TTFB 29ms · load 402ms · LCP 508ms |

### Infrastructure added
- `qa/e2e/db.ts` — a Postgres client (`pg`) for arrange/assert with no API path: seed `broker_accounts`
  balances, point `subscriber_settings` at a trader, seed/inspect `notifications`. Keeps the suite
  self-contained (still no imports from app code).

---

## Findings

**No functional bugs.** Three design-time observations confirmed as behaviors (owner's call):

| # | Confirmed | Type |
|---|---|---|
| OBS-NOTIF-001 | `/notifications` page defines `markAllRead`/`markingAll`/`Spinner` but never renders them — **dead code**; the page has no bulk-read action (only the bell does). | Cleanup |
| OBS-NOTIF-002 | Marking read on the page doesn't re-sync the header badge (no `onChanged`), so the badge can lag until the next poll. | Minor UX |
| OBS-DASH-001 | Realized-P&L KPI + charts depend on `fills` (empty without the poller) — empty-state is the expected local result, not a bug. | By-design |

---

## Coverage notes / deferred

- **SSE live (`NOTIF-SSE-001/002`) not automated:** the e2e backend runs with
  `RUN_BACKGROUND_WORKERS=false`, and notifications only originate from the copy engine / P&L poller —
  there is no create-notification API or event trigger, so `notification.created` can't be emitted
  cleanly. Deferred to a worker-enabled run or manual verification (not faked).
- **Manual / Sprint 8:** responsive viewport matrix, keyboard-only walk, cross-browser (Firefox/WebKit),
  exploratory (tz-near-midnight, negative equity, rapid mark-read).

---

## Production-readiness (Dashboard + Notifications)

**9 / 10.** KPIs compute correctly, data fetching degrades gracefully, notifications are correctly
authorized (IDOR-safe) and a11y-clean, performance is strong. Deductions: SSE live path unverified in
this config + two minor cleanup/UX observations.

## Recommendations
1. Remove the dead `markAllRead`/`Spinner` from `/notifications` (OBS-NOTIF-001) — or add a real
   "Mark all read" button there and wire the header badge (OBS-NOTIF-002).
2. Verify the SSE `notification.created` path in a worker-enabled environment (or Sprint 8).

*Run:* `cd qa && npx playwright test e2e/dashboard.spec.ts e2e/notifications` (stack up — see `qa/README.md`).
