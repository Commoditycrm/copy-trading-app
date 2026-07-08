# Phase 3 — Module Test Design: Sprint 3 · Trader Dashboard + Notifications

**Project:** Copy-Trading Platform · **Module:** Trader Dashboard (`/dashboard`) + Notifications (`/notifications` + header bell + `/api/notifications` + SSE)
**Prepared:** 2026-06-29 · **Status:** DESIGN ONLY. Grounded in source read of
`frontend/hooks/useDashboard.ts`, `frontend/app/(app)/dashboard/page.tsx`,
`frontend/app/(app)/notifications/page.tsx`, `frontend/components/NotificationBell.tsx`,
`backend/app/api/notifications.py`.
**Environment:** LOCAL only.

---

## A. Ground-truth reference (from code)

### Trader Dashboard (`/dashboard`, `useDashboard`)
Aggregates **existing** endpoints in parallel (each degrades to empty on failure):
`GET /api/auth/me`, `/api/positions`, `/api/trades?limit=200`, `/api/brokers`,
`/api/calendar/pnl?from&to&tz` (last 30d), and **trader-only** `/api/subscribers`.

Derived KPIs:
- **Total equity** = Σ `brokers[].total_equity` (sub: "Across N brokers")
- **Realized P&L · 30d** = Σ `dailyPnl[].realized_pnl` (tone-colored)
- **Open positions** = `positions.length` (+ unrealized Σ as delta)
- **4th KPI (trader):** **Active subscribers** = count `subscribers[].copy_enabled` of total; (subscriber view shows **Buying power** — that's Sprint 6)

Charts: **Cumulative realized P&L** area (needs >1 point else empty state), **Daily P&L** bars
(needs a nonzero value else empty). Status row: **BrokerStatusCard** + **RecentExecutions**.
States: loading **skeleton**; **error** card + **Retry** (reload); time-of-day **greeting**.

### Notifications
Backend `/api/notifications` (all `current_user`-scoped):
- `GET ""` — newest-first, `limit` ≤200 (default 50), `unread_only` filter. Returns id/type/message/metadata/read_at/created_at.
- `GET /unread-count` → `{unread}` (bell badge).
- `POST /{id}/read` → **404 if not owner** (IDOR guard); idempotent (only sets if unread).
- `POST /read-all` → `{ok, count}`.

Frontend **/notifications page**: loads `?limit=50`; unread = red border/bg + bold + "Mark read" button;
relative time (+ absolute tooltip, America/New_York); "View in Order History" link when
`metadata.child_order_id`; empty state "You're all caught up"; **optimistic** mark-read (reverts on error);
**SSE `notification.created` prepends** live.

**Header bell** (`NotificationBell`): badge from `unreadCount` prop (AppShell owns it: hydrate+SSE+poll),
**99+** cap; dropdown loads `?limit=20` fresh on open; closes on outside-click + **Escape**; SSE prepend
while open; per-item mark-read; **"Mark all read"**; "View all" → `/notifications`; `aria-haspopup`/
`aria-expanded`/`aria-label`.

---

## B. Discovery observations (to VERIFY — not yet asserted)

| # | Area | Observation |
|---|---|---|
| OBS-NOTIF-001 | /notifications page | Defines `markAllRead`, `markingAll`, imports `Spinner` — but **never renders** a "Mark all read" control. Dead code; the page has no bulk-read action (only the bell does). |
| OBS-NOTIF-002 | /notifications page | Page `markRead` does **not** call back to AppShell, so the **header badge can be stale** after marking read on the page until the next poll/SSE. |
| OBS-DASH-001 | Dashboard | Realized-P&L KPI + both charts read from `/api/calendar/pnl`, which is fed by the `fills` table — empty without the fills poller (per README). So P&L may show 0 / empty states in most local setups. Confirm expected. |
| OBS-DASH-002 | Dashboard | Notifications are primarily subscriber-facing (`copy.retry_failed`, `trader.unfollowed_you`); a **trader** rarely receives any. Notifications UI is still shared and tested generically (seed rows for the logged-in user). |

---

## C. Conventions
- **Test ID:** `DASH-<TYPE>-NNN` / `NOTIF-<TYPE>-NNN`.
- Priority P0–P3 · Severity Crit/High/Med/Low · Automation: Yes-E2E / Yes-API / Yes-DB / Yes-a11y / Yes-perf / Manual.
- **Arrange strategy:** dashboard equity via seeded `broker_accounts` rows (equity fields are stored);
  subscribers via real registered subscriber users following the trader; notifications via **direct DB
  insert** (no create API). All against the local e2e stack.

---

## D. TRADER DASHBOARD (`/dashboard`)

### Functional
| ID | Scenario | Pri | Sev | Preconditions | Expected | Automation |
|---|---|---|---|---|---|---|
| DASH-FUNC-001 | Loads for trader | P0 | High | Trader logged in | KPI row + charts + status render; greeting + "Trader overview" | Yes-E2E |
| DASH-FUNC-002 | Total equity = Σ broker equity | P0 | High | Seed 2 broker_accounts (equity 1000, 2500) | KPI "Total equity" = $3,500; sub "Across 2 brokers" | Yes-E2E + DB |
| DASH-FUNC-003 | Open positions count | P1 | High | No broker positions | "Open positions" = 0; unrealized $0.00 | Yes-E2E |
| DASH-FUNC-004 | Active subscribers KPI | P0 | High | 3 subs follow trader, 2 copy_enabled | "Active subscribers" = 2, sub "2/3 copying" | Yes-E2E + API |
| DASH-FUNC-005 | Greeting by time of day | P3 | Low | — | Morning/afternoon/evening string matches local hour | Yes-E2E |
| DASH-FUNC-006 | Recent executions list | P2 | Medium | Seed a few orders | Recent orders shown | Yes-E2E |

### Business Rule / Boundary
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| DASH-BIZ-001 | Trader sees "Active subscribers" (not "Buying power") | P1 | Med | 4th KPI is Active subscribers for role=trader | Yes-E2E |
| DASH-BND-001 | Zero brokers | P1 | Med | Total equity $0.00; "Across 0 brokers" | Yes-E2E |
| DASH-BND-002 | One broker singular label | P3 | Low | "Across 1 broker" (no plural s) | Yes-E2E |
| DASH-BND-003 | <2 P&L points → empty chart | P2 | Med | Cumulative chart shows "Not enough P&L history yet" | Yes-E2E |
| DASH-BND-004 | All-zero daily P&L → empty bars | P2 | Med | "No realized P&L in this window" | Yes-E2E |

### Negative / Recovery
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| DASH-NEG-001 | `/me` fails (401/5xx) | P1 | High | Error card + Retry button; no crash | Yes-E2E (mock) |
| DASH-RECOV-001 | Sub-fetch fails (e.g. /positions 500) | P1 | High | Page still renders with that section empty (graceful degrade) | Yes-E2E (mock) |
| DASH-RECOV-002 | Retry button reloads | P2 | Low | Clicking Retry re-fetches | Yes-E2E |

### Authorization / Security / DB
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| DASH-AUTHZ-001 | Unauth → login | P1 | High | No token → AppShell bounces to `/login` | Yes-E2E |
| DASH-SEC-001 | No cross-user data | P0 | Crit | Equity/positions/subscribers reflect ONLY the caller (seed a 2nd trader; not visible) | Yes-API + E2E |
| DASH-DB-001 | KPI equity matches DB | P1 | High | Sum of `broker_accounts.total_equity` for user == KPI | Yes-DB |

### UI / UX / Responsive / A11y / Perf
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| DASH-UI-001 | Loading skeleton | P2 | Med | Skeleton blocks show before data | Yes-E2E |
| DASH-UI-002 | KPI formatting/tone | P2 | Med | USD/signed formats; green/red tone on P&L | Yes-E2E |
| DASH-RESP-001 | KPI grid responsive | P2 | Med | 1/2/4 columns at 375/768/1280; charts stack | Yes-E2E |
| DASH-A11Y-001 | axe no serious/critical | P1 | High | 0 serious/critical (watch Recharts SVG + contrast) | Yes-a11y |
| DASH-PERF-001 | Load budget | P3 | Low | Local load within budget with seeded data | Yes-perf |
| DASH-EXPL-001 | Charter | P2 | Low | Large N brokers/orders, negative equity, huge P&L, tz edge (near midnight) | Manual |

---

## E. NOTIFICATIONS (page + bell + API + SSE)

### Functional
| ID | Scenario | Pri | Sev | Preconditions | Expected | Automation |
|---|---|---|---|---|---|---|
| NOTIF-FUNC-001 | Page lists newest-first | P0 | High | Seed 3 notifications | Rendered newest→oldest | Yes-E2E + DB |
| NOTIF-FUNC-002 | Empty state | P1 | Med | 0 notifications | "You're all caught up" | Yes-E2E |
| NOTIF-FUNC-003 | Unread styling | P1 | Med | 1 unread, 1 read | Unread bold + red accent + "Mark read" button | Yes-E2E |
| NOTIF-FUNC-004 | Mark one read (page) | P0 | High | 1 unread | Row flips to read; `read_at` set in DB | Yes-E2E + DB |
| NOTIF-FUNC-005 | "View in Order History" link | P2 | Low | notif w/ `metadata.child_order_id` | Link to `/trades` shown | Yes-E2E |
| NOTIF-FUNC-006 | Bell badge count | P0 | High | 3 unread | Badge shows 3 | Yes-E2E |
| NOTIF-FUNC-007 | Bell dropdown loads on open | P1 | High | seed notifs | Click bell → list (limit 20) | Yes-E2E |
| NOTIF-FUNC-008 | Bell "Mark all read" | P0 | High | 3 unread | All read; badge → 0; DB all `read_at` set | Yes-E2E + API |
| NOTIF-FUNC-009 | Bell closes (outside click + Esc) | P2 | Low | open | Outside click and Escape both close | Yes-E2E |

### API (direct)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| NOTIF-API-001 | list limit + unread_only | P1 | High | `?limit`/`?unread_only=true` honored; `limit>200` clamped/422 | Yes-API |
| NOTIF-API-002 | unread-count | P1 | High | `{unread}` equals DB unread count | Yes-API + DB |
| NOTIF-API-003 | mark read idempotent | P2 | Med | 2nd read call still ok; `read_at` unchanged | Yes-API |
| NOTIF-API-004 | read-all returns count | P1 | Med | `{ok, count}` == number flipped | Yes-API |

### Negative / Authorization / Security (IDOR)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| NOTIF-SEC-001 | Mark other user's notif → 404 | P0 | Crit | User B's notif id via User A → **404** (no cross-user leak) | Yes-API |
| NOTIF-SEC-002 | List scoped to caller | P0 | Crit | User A never sees User B's notifications | Yes-API + DB |
| NOTIF-NEG-001 | Mark non-existent id | P2 | Low | 404 | Yes-API |
| NOTIF-NEG-002 | Unauthenticated calls | P1 | High | 401 on all endpoints without token | Yes-API |
| NOTIF-RECOV-001 | Optimistic revert | P2 | Med | mark-read API fails → row reverts to unread + toast | Yes-E2E (mock) |

### SSE (live)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| NOTIF-SSE-001 | Live prepend on page | P1 | High | `notification.created` event → new item appears without reload | Yes-E2E (emit event) |
| NOTIF-SSE-002 | Badge updates live | P1 | High | New notif → header badge increments | Yes-E2E |

### Boundary / UI / UX / A11y / Responsive
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| NOTIF-BND-001 | Badge 99+ cap | P2 | Low | 100 unread → badge "99+" | Yes-E2E + DB |
| NOTIF-BND-002 | limit=50 page cap | P2 | Low | >50 notifs → only 50 on page | Yes-API |
| NOTIF-UI-001 | Relative + absolute time | P3 | Low | "5m ago" text + ET tooltip | Yes-E2E |
| NOTIF-A11Y-001 | axe + bell ARIA | P1 | High | 0 serious/critical; bell has aria-haspopup/expanded/label; keyboard-openable | Yes-a11y + E2E |
| NOTIF-RESP-001 | Dropdown width mobile | P2 | Low | `min(360px, 100vw-24px)`; no overflow at 375 | Yes-E2E |
| NOTIF-EXPL-001 | Charter | P2 | Low | Rapid mark-read spam, mark-all with 0 unread, very long message wrap | Manual |

---

## F. Traceability summary
| Feature | Func | Biz/Bnd | Neg/Recov | API | DB | Authz/Sec | SSE | UI/UX | A11y | Resp | Perf |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Dashboard | 6 | 5 | 3 | (via UI) | 1 | 2 | — | 2 | 1 | 1 | 1 |
| Notifications | 9 | 2 | 3 | 4 | (in API) | 4 | 2 | 1 | 1 | 1 | — |

**Approx. total: ~52 cases.** Emphasis: dashboard KPI correctness + graceful degradation; notifications
IDOR/authorization (P0) + mark-read correctness + live SSE.

## G. Open questions for sign-off
1. **OBS-DASH-001** — confirm realized-P&L/charts are expected to be empty without the fills poller (so empty-state is the "pass", not a bug).
2. **OBS-NOTIF-001** — is the missing "Mark all read" on the /notifications page intended (bell-only), or should the page have it? (dead code either way)
3. **OBS-NOTIF-002** — acceptable that the header badge lags after marking read on the page until poll?
4. SSE tests: OK to drive `notification.created` by inserting a row + relying on the worker/poll, or should I emit via the events bus directly? (assumed: insert + observe, with a short poll fallback)

---

*End of Phase 3 — Trader Dashboard + Notifications test design.*
