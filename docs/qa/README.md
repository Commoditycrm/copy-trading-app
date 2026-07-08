# QA Engagement — Copy-Trading Platform

End-to-end validation engagement. Local-only execution. No git/CI/AWS actions are performed by QA.

## Artifacts

| Phase | Document | Status |
|---|---|---|
| 1 — Discover | [PHASE1_APPLICATION_UNDERSTANDING.md](PHASE1_APPLICATION_UNDERSTANDING.md) | Complete |
| 2 — QA Planning | [PHASE2_QA_PLANNING.md](PHASE2_QA_PLANNING.md) | Complete |

## Sprint progress

| Sprint | Module(s) | Phase 3 Design | Phase 4 Automation | Phase 5 Execution | Phase 8 Report |
|---|---|---|---|---|---|
| 1 | Authentication (Login, Register, Forgot/Reset Password, Verify Email) | [Complete](sprint1/PHASE3_AUTH_TEST_DESIGN.md) | API/DB/Sec ([suite](../../backend/tests)) · E2E/a11y/perf pending | [Exec 73P/4F](sprint1/PHASE5_AUTH_EXECUTION.md) → fixed → **regression 77P/0F** | [Report](sprint1/PHASE7_8_AUTH_REPORT.md) — 4 bugs fixed |
| 2 | Landing, Contact | — | — | — | — |
| 3 | Trader Dashboard, Notifications | — | — | — | — |
| 4 | Trade Panel, Positions, Order History, Calendar | — | — | — | — |
| 5 | Subscribers, Performance, Broker | — | — | — | — |
| 6 | Subscriber module (Dashboard, Positions, Trades, Calendar, Broker, Settings, Notifications) | — | — | — | — |
| 7 | Admin (Dashboard, Users, Performance, API, Load Test) | — | — | — | — |
| 8 | Full Regression, Security, Performance, Accessibility, Cross-Browser, Production Readiness | — | — | — | — |

## Test-code structure (Hybrid — chosen 2026-06-29)

- **In app repo** (`backend/tests/`, pytest) — unit + API/integration + DB tests. These test code
  internals and are meant to gate every PR in the existing GitHub Actions CI.
- **Top-level `/qa` area** (to be created when E2E work starts) — Playwright E2E, k6 perf, axe a11y.
  Environment-facing suites that run against a deployed/local stack, not a diff. Built self-contained
  so it can be split into its own repo later. QA writes files only; git/CI wiring stays with the owner.

## Workflow (gated)

Discover -> Plan -> per-sprint: Design -> Automate -> Execute -> Bug Report (approval) -> Fix ->
Regression -> Report. STOP after each sprint for approval; no auto-advance.

## Environment note

Execution requires the local stack up: Postgres `:5433`, Redis `:6380`, backend `:8000`, frontend
`:3000`. At discovery time only the frontend was running.
