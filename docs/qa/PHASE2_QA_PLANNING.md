# Phase 2 — QA Planning

**Project:** Copy-Trading Platform (`D:\Workspace\copy-trading-app`)
**Prepared:** 2026-06-29
**Branch:** `qa-branch`
**Backend version:** 0.2.0

> Planning only — no test cases. Cases are authored per-module in Phase 3.
> Secrets are intentionally omitted/redacted throughout this document.

---

## 1. Test Strategy

**1.1 Objective** — Validate the platform end-to-end for correctness, security, resilience, and
performance, with special weight on the money-movement paths (order placement, copy fanout, risk
enforcement) where defects have financial impact.

**1.2 Guiding principles**

- **API is the source of truth.** Client-side role gating is UX-only (no `middleware.ts`); every
  authorization assertion is verified against the backend directly, not just the rendered UI.
- **Never assume success.** Each test asserts actual DB state / API response / broker effect, not
  just HTTP 200.
- **Local-only execution.** All tests run against the local stack (`:3000`/`:8000`, Postgres
  `:5433`, Redis `:6380`). Production is never targeted unless explicitly directed.
- **Deterministic broker behavior.** Use the Fake broker adapter and Alpaca paper keys to avoid
  real-money and flakiness; never route tests through live brokers.
- **Test pyramid:** broad API/integration coverage, focused E2E (Playwright) on critical user
  journeys, targeted unit checks where logic is dense (pnl FIFO, multiplier rounding, bracket math).

**1.3 Test levels**

| Level | Scope | Primary tooling |
|---|---|---|
| Unit | P&L FIFO, multiplier/qty rounding, bracket %->abs, token/crypto helpers | pytest |
| API / Integration | All 60+ endpoints: contract, authz, validation, business rules | pytest + httpx / Postman/Bruno |
| Component (DB) | Row-level assertions, cascades, append-only audit, enum integrity | direct SQL (psycopg) |
| E2E (UI) | Critical journeys per role | Playwright |
| Non-functional | Performance, security, accessibility, responsive, cross-browser | k6, Lighthouse, axe-core, Playwright projects |

**1.4 Test types per module** (applied in Phase 3): Functional, Business Rule, Negative, Boundary,
Validation, UI, UX, Accessibility, Responsive, Cross-Browser, API, Database, Session,
Authorization, Security, Regression, Smoke, Sanity, Performance, Recovery, Exploratory.

**1.5 Environments**

| Env | Use | Notes |
|---|---|---|
| Local dev | All functional/API/E2E execution | Fake broker + Alpaca paper; SendGrid log-only where possible |
| (Reference) AWS Lightsail prod | Read-only awareness | Out of scope for execution unless explicitly authorized |

---

## 2. Test Plan

**2.1 Scope (in)** — All modules in sprint order: Authentication, Landing/Contact, Trader
(dashboard, trade panel, positions, order history, calendar, subscribers, performance, brokers,
notifications), Subscriber (dashboard, positions, trades, calendar, brokers, settings,
notifications), Admin (dashboard, users, performance, api, load test), plus cross-cutting full
regression/security/perf/accessibility.

**2.2 Scope (out)** — Real-money/live-broker trading; production deploy/CI/CD changes; third-party
internals (Alpaca/SnapTrade/SendGrid uptime); production-scale load testing; mobile-native
(responsive web only).

**2.3 Module -> sprint mapping** (one module at a time, STOP between):
S1 Authentication -> S2 Landing+Contact -> S3 Trader Dashboard+Notifications -> S4 Trade
Panel/Positions/Order History/Calendar -> S5 Subscribers/Performance/Broker -> S6 Subscriber
module -> S7 Admin -> S8 Full regression/security/perf/a11y/cross-browser/production-readiness.

**2.4 Deliverables per module** — Test design (Phase 3), automation suite (Phase 4), execution
results + bug report (Phase 5/6), regression results (Phase 7), QA report (Phase 8).

**2.5 Roles & responsibilities** — QA/SDET/SRE (me) authors, automates, executes, reports, and
fixes-on-approval. The user owns all git, GitHub, PRs, Actions, AWS, deploy.

**2.6 Schedule** — Gated by user approvals; no auto-advance between sprints.

---

## 3. Risk Analysis

Severity x Likelihood -> Priority. Financial-impact risks ranked first.

| ID | Risk area | Description | Impact | Likelihood | Priority |
|---|---|---|---|---|---|
| R1 | Copy fanout correctness | Wrong qty (multiplier rounding), duplicate mirror orders, missed subscribers | Critical | Med | P0 |
| R2 | Duplicate orders | Known history of doubling (web-tier listener dup, app/backfill dup) | Critical | Med | P0 |
| R3 | Risk-control enforcement | Daily loss/profit limit, auto-liquidation, TP/SL fail to fire or fire wrongly | Critical | Med | P0 |
| R4 | Authorization bypass | Subscriber hitting trader/admin APIs directly (client gating only) | High | Med | P0 |
| R5 | MLEG / multi-leg parsing | alpaca-py 0.33.0 can't parse MLEG; fix not on this branch | High | High | P1 |
| R6 | Auth/session integrity | Token refresh races, reset-token reuse, verification bypass, rate-limit gaps | High | Med | P1 |
| R7 | Worker SPOF / recovery | Single worker; restart = listener downtime; orphaned PENDING recovery | High | Med | P1 |
| R8 | SSE reliability | Stale connection, 401 loop, token-in-URL leakage, missed events | Med | Med | P1 |
| R9 | Unauth SnapTrade webhook | `/api/brokers/snaptrade/webhook` open; spoof/abuse | High | Low | P1 |
| R10 | Data integrity | Audit-log append-only violated; cascade/SET NULL on broker delete; enum drift | High | Low | P2 |
| R11 | Secrets handling | JWT in localStorage (XSS), Fernet key rotation invalidates creds | High | Low | P2 |
| R12 | Performance regression | Fanout tail latency (the 9s->ms fix), pnl bulk path | Med | Med | P2 |
| R13 | Business-rule gaps | `max_per_contract` UI-only (not server-enforced); one-trader/one-broker constraints | Med | Med | P2 |
| R14 | Email delivery | SendGrid template/sender misconfig -> no reset/verify mail | Med | Med | P2 |
| R15 | UI/UX/a11y | Theme/responsive breakage, missing labels, contrast | Low | Med | P3 |

Mitigation focus: P0/P1 get the deepest negative + boundary + DB-assertion coverage and adversarial
security checks; P2/P3 get functional + regression coverage.

---

## 4. Coverage Matrix (module x test-type planning grid)

Legend: `*` = primary focus, `o` = applicable, `-` = N/A. (Cases authored per-module in Phase 3.)

| Module | Func | Biz | Neg | Bnd | Val | UI/UX | A11y | Resp | API | DB | Sess | Authz | Sec | Perf | Recov |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Authentication | * | * | * | * | * | o | o | o | * | * | * | * | * | o | o |
| Landing/Contact | o | o | * | o | * | * | * | * | o | - | - | o | o | o | - |
| Trader Dashboard | * | * | o | o | o | * | * | * | * | * | o | * | o | * | o |
| Trade Panel | * | * | * | * | * | * | o | * | * | * | o | * | * | * | * |
| Positions | * | * | * | * | o | * | o | * | * | * | o | * | o | o | * |
| Order History | * | * | o | * | o | * | o | * | * | * | o | * | o | * | o |
| Calendar/P&L | * | * | o | * | o | * | o | * | * | * | - | * | o | o | - |
| Subscribers | * | * | * | * | * | * | o | * | * | * | o | * | o | o | o |
| Performance | * | * | o | * | o | * | o | * | * | * | - | * | o | * | o |
| Brokers | * | * | * | * | * | * | o | * | * | * | o | * | * | * | * |
| Subscriber Settings | * | * | * | * | * | * | o | * | * | * | o | * | o | o | o |
| Notifications | * | * | o | o | o | * | o | * | * | * | o | * | o | o | o |
| Admin (all) | * | * | * | * | * | * | o | * | * | * | o | * | * | * | o |
| Copy Engine (cross) | * | * | * | * | o | - | - | - | * | * | - | * | * | * | * |

---

## 5. Test Data Strategy

**5.1 Fixtures (seeded per run, torn down after):**

- 1 trader (`qa.trader@example.test`, business name "QA Capital") — respects the one-trader rule;
  reuse a single trader across subscriber tests.
- N subscribers (`qa.sub.{n}@example.test`) with varied settings: multipliers (0.001, 0.5, 1, 2,
  999.999), with/without daily limits, symbol inclusion/exclusion lists, retry policies, TP/SL %.
- 1 admin (`qa.admin@example.test`).
- Edge accounts: inactive user, unverified-email user, deactivated user.

**5.2 Broker data** — Fake adapter for deterministic fanout/load tests; Alpaca paper keys for
real-integration smoke (options chain, brackets, WebSocket listener). Never live.

**5.3 Market data** — paper symbols (e.g., `AAPL`, `SPY`) and option contracts via Alpaca paper
expiries/strikes; fixed quantities for reproducibility.

**5.4 Sensitive data** — secrets sourced from `backend/.env` (already populated locally); never
logged, never echoed into reports (redact keys). Test passwords are disposable and clearly fake.

**5.5 Data lifecycle** — each suite creates its own users via the register API or a seed script,
asserts, then cleans up (admin load-test cleanup endpoint deletes `fake-load-test-*`; custom QA
users removed by SQL teardown). DB reset between destructive suites via transaction rollback or a
fresh `alembic upgrade head` on a throwaway DB.

**5.6 Idempotency** — seed scripts idempotent; tests independent and order-agnostic.

---

## 6. Entry Criteria

A module enters test execution only when:

1. Phase 1/2 approved; module's Phase 3 design approved.
2. Local stack healthy: Postgres `:5433`, Redis `:6380`, backend `:8000` (`/api/health` = 200),
   frontend `:3000`.
3. `alembic upgrade head` applied cleanly; schema matches models.
4. Test data seeded; Fake broker available; Alpaca paper keys valid.
5. Required env vars present (`DATABASE_URL`, `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`,
   `REDIS_URL`, SnapTrade/SendGrid as needed).
6. Build green: backend imports, frontend `npm run build` (or dev) clean.

---

## 7. Exit Criteria

A module is done when:

1. 100% of designed cases executed (none left untried).
2. 0 open Critical/High defects (P0/P1) in the module; or each has a documented, accepted waiver.
3. >=95% pass rate on Medium/Low after fixes; remaining failures triaged & accepted.
4. Regression (Phase 7) green: prior module suites still pass.
5. Coverage targets met (per matrix); security/a11y findings documented with severity.
6. Phase 8 module report delivered and approved.

---

## 8. Automation Strategy

| Concern | Tool | Approach |
|---|---|---|
| E2E UI | Playwright (TS) | Per-role journeys; reuse the existing frontend; storageState for auth; trace+screenshot+video on failure |
| API | pytest + httpx (preferred, in-repo Python) and/or Bruno/Postman collection | Contract, authz matrix, validation, business rules; run against `:8000` |
| DB verification | psycopg / direct SQL | Assert row state, cascades, audit append-only, enum integrity |
| Accessibility | axe-core (via `@axe-core/playwright`) | Per-page scans on public + key authed pages |
| Performance | k6 (load/fanout) + Lighthouse (page perf) | Fanout latency with Fake broker at N subscribers; page TTI/LCP |
| Unit | pytest | pnl FIFO, multiplier rounding, bracket math, token/crypto |

**Conventions:** tests live under the existing project structure (`backend/tests/` for pytest,
`frontend/e2e/` or `tests/` for Playwright); deterministic seeds; no reliance on wall-clock except
where testing time-based logic (inject/clock-control where possible); CI-agnostic (run locally — CI
and Actions are not touched). Automation candidate flagged per test in Phase 3; high-value
repeatable flows automated first, exploratory/visual kept manual.

---

## 9. Manual Testing Strategy

Reserved for: exploratory testing (creative trade/risk combinations), UX judgment (visual polish,
copy, flows), one-off cross-browser visual checks, complex broker-portal flows (SnapTrade hosted
OAuth), and anything where automation cost outweighs value. Manual sessions are charter-based
(time-boxed, documented findings). Real behavior is observed via Playwright/preview tooling even for
"manual" assertions — never assume.

---

## 10. Regression Strategy

- **Trigger:** after every bug-fix batch (Phase 7) and at Sprint 8 (full).
- **Selection:** re-run the fixed module's full suite + a regression pack of P0/P1 cross-module
  cases (auth, copy fanout no-duplication, risk limits, authorization matrix).
- **Anti-duplication guard:** dedicated regression cases for the doubling history (R2) and listener
  dedup, asserting exactly-once mirror creation.
- **Baseline:** maintain pass/fail baseline per module; any new failure in a previously-green case
  = regression, logged P1+.
- Automated packs re-runnable on demand; results compared to baseline.

---

## 11. Smoke Strategy

A fast (<5 min) build-acceptance suite, run at every Entry-Criteria check:

- `/api/health` 200; frontend root loads.
- Register -> login -> `/api/auth/me` for each role.
- Trader connects Fake/paper broker; places one order; one subscriber mirrors it (exactly one
  child).
- SSE stream connects and receives one event.
- Admin dashboard loads stats.

If smoke fails -> block module execution; report immediately.

---

## 12. Sanity Strategy

Narrow, post-fix checks targeting just the changed behavior + immediate neighbors (e.g., after a
login fix: login success/failure, lockout, refresh — not the whole auth suite). Time-boxed,
shallow, confirms the fix took and nothing adjacent obviously broke before committing to full
regression.

---

## 13. Performance Strategy

| Target | Metric | Tool | Threshold (initial) |
|---|---|---|---|
| Copy fanout | Per-subscriber `pick_lag_ms`, `total_ms` tail at N=50/100/200 (Fake broker) | k6 + Performance API | Flat pick_lag (~50–300 ms), no linear ramp (validates batch-P&L/deferred-flush fix) |
| Order placement | API p95 latency | k6 | < 500 ms (excl. broker) |
| Page load | LCP / TTI on dashboard, trade panel, order history | Lighthouse | LCP < 2.5 s local |
| SSE | event delivery latency, reconnect behavior under drop | Playwright + manual | < 1 s; clean backoff |
| pnl_poller | cycle time at N subscribers | timing logs | within poll interval |

Performance runs use the Fake broker to isolate app cost from broker network time. No production
load testing.

---

## 14. Security Strategy

OWASP-style review aligned to this app's specific surfaces (per Phase 1 observations and the
May-2026 incident doc):

| Area | Focus |
|---|---|
| AuthN | Brute-force/rate-limit (login lockout, register IP throttle), JWT tampering (alg/sig/expiry/role claim), refresh-token rotation & reuse, password reset single-use + fingerprint, email-verification bypass |
| AuthZ | Direct API access to trader/admin endpoints as wrong role (IDOR on `{subscriber_id}`, `{order_id}`, `{account_id}`, `{user_id}`); horizontal + vertical escalation |
| Input | Injection (SQLi via filters/symbol lists/JSONB), XSS in business_name/labels/notifications, mass-assignment on PATCH endpoints |
| Secrets | JWT in localStorage (XSS exfil), SSE token-in-URL leakage, Fernet creds never exposed via API, no secrets in logs/errors |
| Webhooks | `/api/brokers/snaptrade/webhook` unauth — spoofing, replay, malformed payload, abuse |
| Transport/CORS | CORS origin allowlist enforced; no wildcard with credentials |
| Audit | append-only `audit_logs` not mutable via API; sensitive actions logged |
| Network posture | (reference only) Postgres/Redis localhost-bound, Redis password — per hardening doc; prod infra not re-tested unless authorized |

Security testing is non-destructive and confined to the local environment; findings reported with
severity, repro, and affected files. No exploitation of production.

---

## 15. Assumptions & Constraints

- No push/commit/PR/merge/deploy and no Actions/AWS changes — the user owns those.
- Execution is local-only; production untouched unless explicitly authorized.
- Tests use paper/fake brokers — no real money.
- Local stack must be brought up before Phase 4/5 (currently only frontend running).
- Known gap carried forward: MLEG fix not on `qa-branch` (R5).
- Reports redact all secrets.

---

*End of Phase 2 plan.*
