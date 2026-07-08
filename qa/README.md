# QA — E2E / Accessibility / Performance suite

Self-contained, environment-facing test suite for the copy-trading app. It has **no imports from the
app**, so this whole `qa/` folder can be moved into its own repo later with zero rewiring (hybrid
strategy). Unit/API/DB tests live separately in `backend/tests/` (they gate PRs in CI); this suite
runs against a **running stack** and is not tied to a diff.

> Never point this at production. Local only.

## Prerequisites (local stack must be up)

1. **Postgres + Redis** (Docker):
   - `docker run -d --name cta-pg -e POSTGRES_USER=trading -e POSTGRES_PASSWORD=trading -e POSTGRES_DB=trading_app -p 127.0.0.1:5433:5432 postgres:16`
   - `docker run -d --name cta-redis -p 127.0.0.1:6380:6379 redis:7`
   - create the e2e DB: `docker exec cta-pg psql -U trading -d trading_app -c "CREATE DATABASE trading_app_e2e;"`
2. **Backend** on :8000 — from `backend/`, with these env overrides, then `alembic upgrade head` + uvicorn:
   - `DATABASE_URL=postgresql+psycopg://trading:trading@localhost:5433/trading_app_e2e`
   - `REDIS_URL=redis://localhost:6380/2`
   - `RUN_BACKGROUND_WORKERS=false`
   - `SENDGRID_API_KEY=` (blank → reset/verify links are **logged** instead of emailed; the suite reads them)
   - redirect backend stdout/stderr to a log file and point `E2E_BACKEND_LOG` at it.
3. **Frontend** on :3000 — `npm run dev` (proxies `/api` → :8000).

## Run

```bash
cd qa
npm install
npx playwright install chromium
export E2E_BACKEND_LOG=/path/to/backend.log   # where the backend logs the email links
npm run test:auth        # auth module only
npm test                 # everything
npm run report           # open the HTML report
```

Env overrides: `E2E_BASE_URL` (default http://localhost:3000), `E2E_API_URL` (default
http://localhost:8000), `E2E_BACKEND_LOG` (backend log path for reset/verify-token capture).

## Layout

```
qa/
├── playwright.config.ts
├── e2e/
│   ├── helpers.ts          # unique emails, API arrange, email-link capture, token seeding
│   └── auth/               # Sprint 1 — 17 tests (login, register, forgot, reset, verify)
└── (a11y/, perf/ — added in later passes)
```

## Notes / findings surfaced by this suite

- **OBS-E2E-001 (Low):** on the register page, a trader with an empty business name is blocked by the
  field's native `required` attribute, which fires before the JS `notify.error("Business name is
  required for traders")` — so that custom toast is unreachable dead code. Guard still works.
- Test setup sends a unique `X-Forwarded-For` per API arrange-call so bulk seeding doesn't exhaust the
  real per-IP register/login rate limiter.
