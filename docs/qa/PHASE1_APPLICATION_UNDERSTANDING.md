# Phase 1 — Application Understanding Report

**Project:** Copy-Trading Platform (`D:\Workspace\copy-trading-app`)
**Prepared:** 2026-06-29
**Branch under analysis:** `qa-branch` (clean, in sync with `origin/qa-branch`, 10 commits ahead of `main`)
**App version:** backend `0.2.0`

> Secrets are intentionally omitted/redacted throughout this document.

---

## 1. Executive Overview

A **copy-trading platform**: one **trader** places equity/option orders; every active
**subscriber**'s linked broker account mirrors them, scaled by a per-subscriber multiplier and
gated by risk controls. An **admin** tier monitors fanout latency, broker/listener health, runs
load tests, and tunes runtime knobs.

| Layer | Technology |
|---|---|
| Frontend | Next.js 15.0.3 (App Router) · React 19 RC · TypeScript 5.6 · Tailwind 3.4 · Recharts · Framer Motion · react-toastify |
| Backend | FastAPI 0.115 · SQLAlchemy 2.0 · Alembic · Pydantic 2 · Uvicorn |
| Database | PostgreSQL 16 |
| Cache / Bus | Redis 5 (cache, pub/sub, rate-limit, SnapTrade session, listener state) |
| Auth | JWT access (30 min) + refresh (14 d), HS256; bcrypt passwords |
| Secrets | Fernet-encrypted broker credentials at rest |
| Brokers | Alpaca (direct, ready), SnapTrade (aggregator, ready), IBKR (direct, ready), Fake (load-test), Webull (removed -> via SnapTrade) |
| Email | SendGrid (dynamic templates: password reset + verification) |
| Deploy | AWS Lightsail · Docker Compose (web + worker + frontend + Postgres + Redis + Caddy) · GitHub Actions on push to `main` |

---

## 2. User Roles & Authorization Model

| Role | Lands on | Capabilities |
|---|---|---|
| Trader | Trade Panel | Place orders (fan out to subscribers), manage subscribers, view performance/fanout latency, master copy on/off switch. Only one trader allowed per the first-run flow. |
| Subscriber | Positions | Follow a trader, mirror orders, configure risk controls (limits, TP/SL, filters, retry), personal copy on/off. |
| Admin | Admin Dashboard | User management, role/activation changes, load-test seeding, runtime config tuning, broker/listener health, test-result tracking. |

**Enforcement:**

- **Server-side (authoritative):** dependency guards `require_trader` / `require_subscriber` /
  `require_admin` in `backend/app/api/deps.py`; `current_user` also checks `is_active`. 403 on
  role mismatch.
- **Client-side (UX only):** no `middleware.ts` — gating is in layouts. `app/(app)/layout.tsx`
  (AppShell) and `app/admin/layout.tsx` each fetch `/api/auth/me` on mount and redirect by role.
  Trader-only pages rely on nav not rendering rather than a hard route guard.
  *(Flagged for Phase 2 authorization testing — direct URL/API access to trader-only endpoints by
  a subscriber must be tested against the API, not just the UI.)*

---

## 3. Page Inventory

### 3.1 Public (no auth)

| Route | File | Purpose |
|---|---|---|
| `/` | `app/page.tsx` | Role-aware redirect (-> dashboard / admin / login) |
| `/login` | `app/login/page.tsx` | Email+password sign-in; email lowercased |
| `/register` | `app/register/page.tsx` | Trader/Subscriber signup; traders require business name |
| `/forgot-password` | `app/forgot-password/page.tsx` | Request reset link (no email enumeration) |
| `/reset-password` | `app/reset-password/page.tsx` | Complete reset via emailed token |
| `/verify-email` | `app/verify-email/page.tsx` | Confirm email via token (idempotent) |
| `/contact` | `app/contact/page.tsx` | Public contact form (Formspree / mailto fallback) |

### 3.2 Trader (route group `(app)`)

`/dashboard` · `/trade-panel` · `/positions` · `/trades` (order history) · `/calendar` (P&L) ·
`/subscribers` · `/performance` · `/brokers` · `/notifications`

### 3.3 Subscriber (route group `(app)`, role-differentiated content)

`/dashboard` · `/positions` · `/trades` · `/calendar` · `/brokers` · `/settings` (risk controls) ·
`/notifications`

### 3.4 Admin (route prefix `/admin`)

`/admin/dashboard` · `/admin/users` · `/admin/performance` · `/admin/api` (runtime tunables) ·
`/admin/load-test`

> Module-name mapping: "trade panel / positions / order history / calendar" map to `/trade-panel`,
> `/positions`, `/trades`, `/calendar`. Subscriber "order history / trades" = `/trades`. All
> requested pages exist.

---

## 4. Navigation

- **Sidebar (`components/AppShell.tsx`)** — role-driven menus:
  - Trader: Dashboard, Trade Panel, Positions, Order History, P&L, Subscribers, Performance, Broker
  - Subscriber: Dashboard, Positions, Order History, P&L, Broker, Settings
  - Footer: copy-trading master/personal on-off toggle + Sign out
- **Admin sidebar (`app/admin/layout.tsx`):** Dashboard, Users, Load Test, Performance, API
- **Header:** broker ListenerPill, SSE status pill, theme toggle, NotificationBell (unread badge,
  SSE-synced), user avatar; unverified-email banner (soft, non-blocking, with Resend).
- **Responsive:** static sidebar on desktop (collapsible, persisted to localStorage); mobile drawer.

---

## 5. Authentication

- **Tokens:** JWT access (30 min) + refresh (14 d), HS256. Access carries `sub`, `role`, `type`,
  `iat`, `exp`.
- **Client storage:** `localStorage` keys `trading-app:access` / `trading-app:refresh` (no
  cookies). User cached in `sessionStorage` (`trading-app:user`). *(localStorage tokens -> XSS
  exposure surface; note for Phase 2 security.)*
- **Refresh flow (`lib/api.ts`):** on 401, single coalesced refresh; retries original request; on
  refresh failure clears tokens -> `/login`.
- **SSE auth:** `/api/events?token=<access>` (query param, since EventSource can't set headers —
  token-in-URL noted for security review).
- **Password reset:** single-use token bound to an HMAC fingerprint of the current password hash,
  30-min TTL — link self-invalidates on password change.
- **Email verification:** 24-h token bound to the email; soft-enforced (login allowed unverified,
  banner shown).
- **Rate limiting (Redis):** per-IP on register; per-email + per-IP on login with lockout (429 +
  Retry-After).

---

## 6. Business Flows (core)

1. **Onboarding:** Register trader (one only) -> register subscribers -> each connects a broker ->
   trader flips master switch ON -> subscriber follows trader + flips copy ON.
2. **Copy fanout (`services/copy_engine.fanout_async`):** trader order detected -> eligibility
   filters (copy_enabled, daily-loss-limit, symbol filters, multiplier qty>0) -> per-iteration
   (<75 subs) or batched (>=75) place_order via `asyncio.gather` -> child orders linked by
   `parent_order_id` -> SSE events per child -> retries on transient broker errors. Phase-1 P&L
   batched + deferred flush for latency.
3. **Order ingestion (listeners):** Alpaca WebSocket realtime + SnapTrade/IBKR pollers reconcile
   broker orders/fills into `orders`/`fills`, gated by per-broker listener flags (auto_pull /
   open / filled).
4. **Risk enforcement (`pnl_poller`, ~10–60 s):** daily loss/profit limits (USD or % of day-start
   equity) -> auto-pause copy; per-position TP/SL -> auto-close; equity floor -> auto-liquidate +
   sticky disable.
5. **Bracket handling:** Alpaca native OCO; emulated TP/SL legs on other brokers anchored to actual
   fill price.

---

## 7. API Surface (60+ endpoints, 12 routers)

| Router | Prefix | Highlights |
|---|---|---|
| auth | `/api/auth` | register, login, refresh, me, forgot/reset-password, verify/resend-email |
| brokers | `/api/brokers` | connect/list/delete, refresh-balance, listener settings, SnapTrade start/finish, **unauth** `/snaptrade/webhook` |
| trades | `/api` | list/detail/place/cancel orders, stats, bracket update, close-reasons |
| subscribers | `/api/subscribers` | list, copy-state toggle, multiplier, remove (trader-only) |
| settings | `/api/settings` | subscriber risk controls (limits, TP/SL, filters, retry, follow-trader) |
| positions | `/api/positions` | list, close-all, close one |
| performance | `/api/performance` | trader fanout latency breakdown |
| admin | `/api/admin` | stats, users CRUD, load-test seed/cleanup, fanout/broker perf, config knobs, health |
| options | `/api/options` | expiries, strikes (Alpaca) |
| listener | `/api/listener` | listener status |
| notifications | `/api/notifications` | list, unread-count, mark read |
| events | `/api/events` | SSE stream |
| (health) | `/api/health` | unauthenticated liveness |

---

## 8. Database (11 tables)

`users`, `broker_accounts`, `orders` (self-referencing parent/child for copies), `fills`,
`trader_settings`, `subscriber_settings`, `notifications`, `audit_logs` (append-only),
`daily_equity_snapshots`, `test_results`, `load_test_runs`. Alembic-managed; FKs use CASCADE /
SET NULL (order history preserved when a broker is removed).

---

## 9. Redis Usage

Subscriber/broker caches (60 s / 300 s TTL), listener state (cross-process mirror), platform-config
overrides (fanout threshold, P&L poll interval), rate-limit counters, SnapTrade connect sessions
(`snaptrade:connect:{user_id}`, 30 min), pub/sub for listener control + SSE.

---

## 10. Third-Party Services & Dependencies

- **Alpaca** (`alpaca-py 0.33.0`) — direct keys, WebSocket. **Note:** the multi-leg (MLEG) parsing
  fix (bump to 0.43.4) tracked in prior sessions is **not merged into `qa-branch`** — MLEG spreads
  placed at the broker may not parse.
- **SnapTrade** (`snaptrade-python-sdk 11.0.197`) — hosted OAuth portal + webhook.
- **IBKR** — OAuth 1.0a poller.
- **SendGrid** — reset + verification templates.
- **PostgreSQL / Redis** — local via Docker (ports 5433 / 6380 per `.env`); prod localhost-bound +
  password-protected per security-hardening doc.

---

## 11. Worker / Web Split & Background Jobs

- **Web tier** (`RUN_BACKGROUND_WORKERS=false`): HTTP + request-path fanout, multiple uvicorn
  workers.
- **Worker tier** (`RUN_BACKGROUND_WORKERS=true`, **never scale >1**): crash-recovery sweep, broker
  listeners (x3 adapters) + reconciler (15 s), P&L poller, retry scheduler (10 s). Web<->worker
  coordinate via Redis pub/sub (`listener_control`).

---

## 12. Environment State (at time of discovery)

| Component | Local status |
|---|---|
| Frontend dev server | Running — node on `:3000` |
| Backend (`:8000`) | Not running |
| Postgres (`:5433`) / Redis (`:6380`) | Not running — no Docker containers up |
| Docker | Available, no containers started |
| Production | AWS Lightsail (Caddy/TLS), auto-deploy on push to `main` |

> To execute test phases locally the full stack must be up: `docker compose up -d` for
> Postgres/Redis, `alembic upgrade head`, backend on `:8000`, frontend on `:3000`. Production is not
> tested unless explicitly directed.

---

## 13. Early Discovery Observations (carried into Phase 2 risk analysis)

1. Client-side route guards are UX-only; API-level authorization is the real boundary — must be
   tested directly.
2. JWT in `localStorage` + SSE token in URL query — security review candidates.
3. `/api/brokers/snaptrade/webhook` is unauthenticated by design — needs signature/abuse testing.
4. MLEG fix not on this branch — known broker-parsing gap.
5. `max_per_contract` is UI-only, not server-enforced — business-rule gap.
6. Worker is a single point of failure (must never scale >1); restart = listener downtime bridged
   by Redis.
7. Email verification is soft-enforced — confirm intended.
8. One-trader / one-broker-per-user constraints — boundary/negative test targets.

---

*End of Phase 1 report.*
