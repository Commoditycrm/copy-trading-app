# Sprint 4 · Trade Panel · Positions · Order History · Calendar — Automation + Execution + Report

**Date:** 2026-07-06 · **Module:** the trading core (order placement, positions, order history, P&L calendar)
**Layers:** API/DB (pg-seeded) · **real placement via the Fake broker adapter** · E2E (Playwright/Chromium) · a11y (axe)
**Suite:** `qa/e2e/trade/`, `qa/e2e/order-history/`, `qa/e2e/positions/`, `qa/e2e/calendar/`, `qa/e2e/a11y/sprint4.a11y.spec.ts`
**Environment:** LOCAL — frontend :3000, backend :8000 (running `main` code, `RUN_BACKGROUND_WORKERS=false`), e2e DB `trading_app_e2e`. **Paper/fake brokers only. Production untouched.**

---

## Result

**36 passed · 1 failed.** The single failure is a genuine accessibility defect (BUG-A11Y-002), not a test error.

| Area | Tests | Pass | Fail |
|---|---|---|---|
| Placement API (authz, validation, broker-state, **real fake-broker placement**, dedup) | 11 | 11 | 0 |
| Order History API (list, stats all/mine + notional, bracket-stats rule, detail+fills, cancel 409/404, live cancel) | 8 | 8 | 0 |
| Order History UI (renders orders + Filled-notional tile) | 1 | 1 | 0 |
| Positions API (empty, close 404, close-all, close-all-subscribers authz) | 6 | 6 | 0 |
| Calendar API (empty, from>to 422, view-as 403/404, **realized P&L from seeded round-trip**) | 5 | 5 | 0 |
| Trade Panel UI (renders toggle+CTAs; disabled "Connect a broker first") | 2 | 2 | 0 |
| a11y (positions, trades, calendar) | 3 | 3 | 0 |
| a11y (trade-panel) | 1 | 0 | **1 → BUG-A11Y-002** |

### Breakthrough: real order placement locally
The Fake broker adapter accepts placement, so `POST /api/trades` was exercised **end-to-end** (201,
`broker_order_id: fake-…`) for stock + option, plus **duplicate-suppression** (two concurrent
identical POSTs returned the same order id) and a **live cancel** — no Alpaca account needed.

---

## Findings

### BUG-A11Y-002 (Medium) — Trade Panel has a form control without a label + contrast issues
- axe on `/trade-panel`: **critical `label` (1)** — a form element has no programmatic label; **serious
  `color-contrast` (2)**. Same class as the auth BUG-A11Y-001 (inputs rely on placeholders).
- **Impact:** screen-reader users can't identify the field; low-contrast text fails WCAG AA.
- **Fix (app code, separate branch):** associate the unlabeled input (`<label htmlFor>`/`aria-label`)
  and raise the two low-contrast foregrounds. Exact node is in the test's `axe-violations.json` artifact.
- The other three trading pages (positions, trades, calendar) are **a11y-clean**.

### OBS-S4-ENUM (Low) — stray `retry_pending` enum label
`order_status` in Postgres has **both** `retry_pending` (lowercase) and `RETRY_PENDING` — a latent
enum-drift duplicate (same class as the admin-enum bug). Cosmetic today; worth cleaning to avoid a
future insert using the wrong casing.

### OBS-OH-001 (corrected) — bracket-leg visibility
The **API `GET /api/trades` returns all orders including resting bracket legs** (verified). The hiding
of resting TP/SL legs happens **client-side in the UI**, and the **stats** endpoint excludes non-filled
legs from its counts. (The Phase-3 design note, taken from an exploration summary, conflated list vs
stats — corrected here. Not a bug.)

---

## Coverage notes / deferred
- **Needs Alpaca paper (deferred):** native-bracket stock orders, the options chain endpoints
  (`/api/options/*` are Alpaca-only → 501 on fake), real fanout to subscribers, emulated bracket-leg
  placement on fill.
- **Async bulk (queued) subscriber close/cancel:** only the immediate `queued_*` response is asserted,
  not the background broker work (by design — no worker in this config).
- **Sprint 8:** cross-browser (Firefox/WebKit), responsive matrix, Lighthouse, UI-level bracket-geometry
  warnings.

---

## Production-readiness (trading core)

**8.5 / 10.** Order placement is correctly authorized (traders only), thoroughly validated, dedup-safe,
and places/cancels end-to-end; order history + stats + calendar P&L compute correctly and are
IDOR-safe; positions endpoints are correctly gated. Deductions: the trade-panel a11y defect
(BUG-A11Y-002) and the broker-dependent paths verified only via the Fake adapter (not Alpaca).

## Recommendations
1. Fix BUG-A11Y-002 (label + contrast) on a frontend branch — mirrors the auth a11y fix.
2. Clean the stray `retry_pending` enum label (OBS-S4-ENUM).
3. Run the broker-dependent placement paths against an Alpaca paper account before go-live.

*Run:* `cd qa && npx playwright test e2e/trade e2e/order-history e2e/positions e2e/calendar` (stack up).
