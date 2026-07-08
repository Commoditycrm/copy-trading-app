# Phase 3 — Module Test Design: Sprint 4 · Trade Panel · Positions · Order History · Calendar

**Project:** Copy-Trading Platform · **Module:** the trading core (order placement, positions, history, P&L calendar)
**Prepared:** 2026-06-29 · **Status:** DESIGN ONLY. Grounded in a deep source read (2 exploration passes) of
`trade-panel/page.tsx`, `positions/page.tsx`, `trades/page.tsx`, `calendar/page.tsx`,
`backend/app/api/trades.py`, `positions.py`, `options.py`, `schemas/order.py`, `models/order.py`.
**Environment:** LOCAL only. **Paper/fake brokers only — never live, never real money.**

---

## A. ⚠️ Central constraint: order *execution* needs a broker

`POST /api/trades` (and all close/cancel-at-broker paths) call the broker adapter. Our e2e stack has
**no connected real broker**, and a seeded `broker_accounts` row has a placeholder credential that
won't decrypt/authenticate. Therefore:

- **Automatable now (no broker):** all **schema validation** (422), **authorization** (`require_trader`
  403 / 401), **broker-state checks** (404 not-found / 409 not-connected), **Order History + stats +
  cancel-validation** (via DB-seeded orders), **Calendar** (seeded fills / authz / 422), and **all UI
  form behavior + client-side gates** (the panel blocks before any network call).
- **Needs a broker (deferred or Fake-adapter):** a real placement that reaches `status=submitted/filled`,
  fanout to subscribers, bracket-leg emulation, real cancel/close at broker.

**Plan for the happy path:** attempt to route through the **Fake** adapter — seed a `broker='fake'`
account with Fernet-valid creds (encrypted via the app's own key in a one-off helper). If the place
path accepts fake, we get real end-to-end placement locally. If not, those cases are marked
**deferred (needs Alpaca paper)** — not faked.

---

## B. Ground-truth endpoint map (from code)

### Trade Panel / placement (`require_trader` unless noted)
| Endpoint | Notes |
|---|---|
| POST `/api/trades?broker_account_id=` | `PlaceOrderIn`; 201 `OrderOut`; **require_trader**; 3s dup-suppression + advisory lock; 409 `trading_disabled`/`broker_not_connected`, 404 `broker_account_not_found`, 422 validation, 502 `broker_error` |
| GET `/api/options/expiries` · `/strikes` · `/quote` | `account_id`+`symbol`(+expiry/strike/right); Alpaca-only (501 otherwise); 404/502 |
| PATCH `/api/trades/{id}/bracket` | require_trader; geometry 422; 409 leg/disconnect/partial; 501 alpaca-native |

**PlaceOrderIn rules:** option ⇒ expiry+strike+right required; limit/stop_limit ⇒ limit_price; stop/stop_limit ⇒ stop_price; bracket only on market/limit; buy bracket `SL<limit<TP`, sell `TP<limit<SL`; symbol 1–40; quantity>0; prices>0.

### Positions (`current_user` unless noted)
| Endpoint | Notes |
|---|---|
| GET `/api/positions` | aggregate across connected accounts; skips disconnected/flaky silently |
| POST `/api/positions/close-all?include_subscribers=` | reverse-market each; per-item failures don't abort; 200 with closed/failed lists |
| POST `/api/positions/close-all-subscribers` | **require_trader**; async, returns `queued_pairs` |
| POST `/api/positions/{broker_symbol}/close?broker_account_id=` | 404 acct/pos, 409 not-connected, 422 qty≤0 / qty>position |

### Order History (`current_user`)
| Endpoint | Notes |
|---|---|
| GET `/api/trades?limit&from&to` | ≤1000; newest first; **hides bracket exit legs unless FILLED** |
| GET `/api/trades/stats?from&to` | scopes `all` vs `mine` (trader: mine = not fanned_out); counts + notional |
| GET `/api/trades/{id}` | 404 if not owner |
| POST `/api/trades/{id}/cancel` | cancellable = pending/submitted/accepted/partially_filled; 409 else; 502 broker; trader→cascade |
| POST `/api/trades/cancel-all-open?include_subscribers=` | 200 with counts |
| POST `/api/trades/cancel-all-subscribers-open` | require_trader; async |

### Calendar (`current_user`)
| Endpoint | Notes |
|---|---|
| GET `/api/calendar/pnl?from&to&tz&user_id` | daily realized P&L from fills; 422 `from>to`; **view-as**: 403 `trader_only` / 404 `not_a_subscriber` |
| POST `/api/trades/sync-fills` | pulls latest fills (best-effort) |

**Statuses:** pending, submitted, accepted, partially_filled, filled, canceled, rejected, expired, retry_pending. Working/cancellable = first four.

---

## C. Discovery observations (to VERIFY)
| # | Note |
|---|---|
| OBS-TP-001 | `POST /api/trades` is **require_trader** — subscribers cannot place orders directly (they only mirror). A subscriber hitting it → 403. Confirm intended (it is, by design). |
| OBS-TP-002 | 3s duplicate-suppression window + Postgres advisory lock — a rapid double-submit returns the SAME order, not two. |
| OBS-TP-003 | Options brackets are **emulated** (Alpaca rejects complex option orders); Alpaca **stocks** use native brackets and can't be modified post-fill (501). |
| OBS-OH-001 | Bracket exit legs are hidden from history/stats until `FILLED` — deliberate. |
| OBS-CAL-001 | Calendar depends on `fills`; empty without the poller → empty grid (expected, not a bug — cross-refs OBS-DASH-001). |
| OBS-POS-001 | `close-all` is unsigned per-item; partial broker failures return 200 with a `failed` list (no 5xx). |

---

## D. TRADE PANEL — order placement

### Authorization / broker-state (automatable now)
| ID | Scenario | Pri | Sev | Expected | Automation/arrange |
|---|---|---|---|---|---|
| TP-AUTHZ-001 | Subscriber POSTs /api/trades | P0 | Crit | 403 (require_trader) | Yes-API (subscriber token) |
| TP-AUTHZ-002 | Unauthenticated POST | P1 | High | 401 | Yes-API |
| TP-NEG-001 | Trader + unknown broker_account_id | P1 | High | 404 `broker_account_not_found` | Yes-API |
| TP-NEG-002 | Trader + disconnected broker | P1 | High | 409 `broker_not_connected` | Yes-API + seed disconnected broker |
| TP-BIZ-001 | Trading disabled (kill switch) | P1 | High | 409 `trading_disabled` | Yes-API + set trader_settings.trading_enabled=false |

### Schema validation (422 — automatable now, before broker)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| TP-VAL-001 | Option missing strike | P0 | High | 422 | Yes-API |
| TP-VAL-002 | Option missing expiry/right | P1 | High | 422 | Yes-API |
| TP-VAL-003 | limit order, no limit_price | P0 | High | 422 | Yes-API |
| TP-VAL-004 | stop order, no stop_price | P1 | Med | 422 | Yes-API |
| TP-VAL-005 | quantity ≤ 0 | P0 | High | 422 | Yes-API |
| TP-VAL-006 | symbol > 40 chars | P2 | Low | 422 | Yes-API |
| TP-VAL-007 | bracket on stop entry | P2 | Med | 422 (bracket only market/limit) | Yes-API |
| TP-BND-001 | buy bracket SL≥limit or limit≥TP | P1 | High | 422 buy-geometry | Yes-API |
| TP-BND-002 | sell bracket geometry invalid | P1 | High | 422 sell-geometry | Yes-API |

### Placement happy paths (needs broker — Fake adapter or deferred)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| TP-FUNC-001 | Stock market buy | P0 | Crit | 201; order persisted; status submitted/filled | Fake-adapter / deferred |
| TP-FUNC-002 | Stock limit buy + native bracket | P0 | Crit | 201; TP/SL stored; exit legs (Alpaca native) | Deferred (Alpaca paper) |
| TP-FUNC-003 | Option market buy | P0 | Crit | 201; OCC built; option fields stored | Fake / deferred |
| TP-FUNC-004 | Option limit + emulated bracket | P1 | High | 201; emulator places exit legs on fill | Deferred |
| TP-FUNC-005 | Duplicate double-submit (OBS-TP-002) | P1 | High | 2 rapid identical POSTs → one order returned twice | Fake / deferred |
| TP-FUNC-006 | Fanout marks fanned_out=true | P1 | High | order.fanned_out_to_subscribers=true when copy on | Fake / deferred + DB |

### UI form behavior (automatable now — client gates fire before network)
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| TP-UI-001 | Panel renders (4 CTAs, instrument toggle, popular symbols) | P1 | Med | Buy/Sell × MKT/LMT buttons; Option/Stock toggle; symbol pills | Yes-E2E |
| TP-UI-002 | "Connect a broker first" gate | P0 | High | With no broker, clicking a CTA warns, no POST | Yes-E2E |
| TP-UI-003 | Options: MKT gated on expiry+strike | P1 | Med | "Select an expiry and strike first" | Yes-E2E |
| TP-UI-004 | Limit gated on limit_price>0 | P1 | Med | "Enter a limit price" | Yes-E2E |
| TP-UI-005 | Instrument toggle shows/hides contract fields | P2 | Low | Option → expiry/strike/right; Stock → qty only | Yes-E2E |
| TP-UI-006 | Cost estimate + OCC preview | P3 | Low | qty×price×mult; OCC string when contract complete | Yes-E2E |
| TP-UI-007 | Bracket %→$ live conversion + geometry warn | P2 | Med | client warns SL≥100% / TP≤entry before POST | Yes-E2E |

### PATCH bracket
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| TP-BRK-001 | Modify exit leg directly | P2 | Med | 409 `cannot_modify_bracket_leg` | Yes-API + seed leg |
| TP-BRK-002 | Geometry invalid on modify | P2 | Med | 422 | Yes-API + seed entry |
| TP-BRK-003 | Modify order not owned | P2 | Med | 404 | Yes-API |

---

## E. POSITIONS
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| POS-FUNC-001 | Empty positions (no broker) | P1 | Med | `[]`; page "no open positions" | Yes-API + E2E |
| POS-FUNC-002 | Page renders BulkExitBar (role-gated) | P1 | Med | trader sees subscriber-exit chips; subscriber doesn't | Yes-E2E |
| POS-NEG-001 | close position, unknown account | P1 | High | 404 `broker_account_not_found` | Yes-API |
| POS-NEG-002 | close, qty ≤ 0 | P2 | Med | 422 `quantity_must_be_positive` | Yes-API + seed broker |
| POS-AUTHZ-001 | close-all-subscribers as subscriber | P1 | High | 403 (require_trader) | Yes-API |
| POS-FUNC-003 | close-all with no positions | P2 | Low | 200 `closed_count=0` | Yes-API |
| POS-FUNC-004 | close a real position | P0 | Crit | reverse order placed | Fake / deferred |
| POS-A11Y-001 | axe on /positions | P1 | High | 0 serious/critical | Yes-a11y |
| POS-RESP-001 | table responsive | P2 | Low | scroll container at 375 | Yes-E2E |

## F. ORDER HISTORY
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| OH-FUNC-001 | List newest-first | P0 | High | seeded orders, submitted_at desc | Yes-API+DB |
| OH-FUNC-002 | limit + from/to filter | P1 | High | respects params; to exclusive | Yes-API+DB |
| OH-FUNC-003 | stats all vs mine | P0 | High | mine = not fanned_out (trader); notional = Σ filled×price×mult | Yes-API+DB |
| OH-FUNC-004 | bracket exit leg hidden unless filled (OBS-OH-001) | P1 | Med | resting leg absent; filled leg present | Yes-API+DB |
| OH-FUNC-005 | detail + fills | P2 | Med | GET {id} returns fills; 404 non-owner | Yes-API+DB |
| OH-FUNC-006 | cancel terminal order | P1 | High | 409 `not_cancellable` | Yes-API+DB (seed FILLED) |
| OH-FUNC-007 | cancel non-owner order | P1 | High | 404 | Yes-API+DB |
| OH-SEC-001 | history scoped to caller | P0 | Crit | user A never sees user B's orders | Yes-API+DB |
| OH-UI-001 | page renders tabs/columns/summary | P1 | Med | All/My tabs (trader), Filled notional tile | Yes-E2E+DB |
| OH-UI-002 | symbol search filter | P2 | Low | filters rows client-side | Yes-E2E+DB |
| OH-A11Y-001 | axe on /trades | P1 | High | 0 serious/critical | Yes-a11y |

## G. CALENDAR
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CAL-FUNC-001 | Empty range | P1 | Med | `[]`; grid all "—" | Yes-API+E2E |
| CAL-FUNC-002 | Daily P&L from seeded fills | P1 | High | day/realized_pnl/trade_count correct | Yes-API+DB (seed orders+fills) |
| CAL-VAL-001 | from > to | P1 | Med | 422 | Yes-API |
| CAL-SEC-001 | non-trader view-as another | P0 | Crit | 403 `trader_only` | Yes-API |
| CAL-SEC-002 | trader view-as non-follower | P1 | High | 404 `not_a_subscriber` | Yes-API |
| CAL-FUNC-003 | tz shifts day boundary | P2 | Med | fill near midnight buckets per tz | Yes-API+DB |
| CAL-UI-001 | heatmap renders + drill link | P2 | Med | click day → /trades?from=&to= | Yes-E2E+DB |
| CAL-A11Y-001 | axe on /calendar | P1 | High | 0 serious/critical | Yes-a11y |

## H. Cross-cutting
| ID | Scenario | Pri | Automation |
|---|---|---|---|
| S4-PERF-001 | /trades, /positions, /calendar load budgets | P3 | Yes-perf |
| S4-RESP-001 | responsive at 375/768/1280 | P2 | Yes-E2E |
| S4-XBROWSER-001 | Firefox/WebKit (Sprint 8) | P3 | Deferred |

---

## I. Traceability summary
| Feature | automatable now | needs broker |
|---|---|---|
| Trade Panel | authz(2)+broker-state(3)+validation(9)+UI(7)+bracket-API(3) = **24** | placement happy paths (6) |
| Positions | 6 | 1 |
| Order History | 11 | — (cancel-at-broker real: deferred) |
| Calendar | 8 | — |
| Cross-cutting | perf/resp/a11y | xbrowser (S8) |

**~55 cases automatable now; ~8 need a broker (Fake-adapter attempt, else deferred).**

## J. Open questions for sign-off
1. **Fake-adapter placement:** OK to seed a `broker='fake'` account (Fernet-valid creds via a one-off) to exercise *real* order placement locally, or keep placement **deferred to Alpaca paper**? (I'll attempt Fake; fall back to deferred.)
2. **OBS-TP-001** confirm subscribers are intended to get 403 on direct placement (mirror-only).
3. Bulk subscriber close/cancel (async, `queued_*`) — test the queued-count response only (not the background broker work), OK?
4. Priority within the sprint: I'll lead with the **P0** authz + validation + order-history/stats + calendar-authz (highest value, all automatable now).

---

*End of Phase 3 — Sprint 4 test design.*
