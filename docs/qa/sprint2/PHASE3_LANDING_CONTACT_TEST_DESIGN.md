# Phase 3 — Module Test Design: Sprint 2 · Landing + Contact

**Project:** Copy-Trading Platform · **Module:** Landing (root `/`) + Contact (`/contact`)
**Prepared:** 2026-06-29 · **Status:** DESIGN ONLY. Grounded in source read of
`frontend/app/page.tsx` (root redirect) and `frontend/app/contact/page.tsx`.
**Environment:** LOCAL only.

---

## A. Ground-truth reference (from code)

### Landing — root `/` (`frontend/app/page.tsx`)
- Renders `null`; a `useEffect` decides the destination:
  - no access token → `router.replace("/login")`
  - has token → `GET /api/auth/me` → `admin` → `/admin`, else → `/dashboard`
  - `/me` returns 401 → `clearTokens()` → `/login`
- **There is no marketing/home page.** Every "Home" / "Back to home" link resolves to `/`, which
  immediately redirects (anonymous visitors land on `/login`).

### Contact — `/contact` (`frontend/app/contact/page.tsx`)
- **Public** (outside the `(app)` auth group). No auth required.
- Fields: **Name** (required, `autocomplete=name`), **Email** (required, `type=email`), **Message**
  (required `textarea`). All labels associated via `<Field htmlFor>` + input `id`.
- `FORM_ENDPOINT = ""` → **mailto fallback**: builds `mailto:support@kopyya.com?subject=…&body=…`
  (both `encodeURIComponent`-escaped), sets `window.location.href`, marks status `sent`, resets form.
  **No network request, no backend, no DB.**
- If `FORM_ENDPOINT` were set → `POST` FormData to it (Formspree); `sending`→`sent`/`error` states.
- Status slot: `sent` = success copy; `error` = message + `mailto:` fallback link.
- Footer: `© <current year> ARK`, support email mailto, Home link. Brand wordmark hardcoded "ARK".

---

## B. Discovery observations (to VERIFY in execution — not yet asserted)

| # | Area | Observation |
|---|---|---|
| OBS-LAND-001 | Landing | No public marketing/home page exists — `/` is a pure redirect. "Home"/"Back to home" links on Contact send anonymous users to `/login`, not a home page. Confirm this is intended (SnapTrade review just needs a reachable path). |
| OBS-CONTACT-001 | Contact UX | mailto fallback marks status **"sent"** ("your message is on its way") immediately after setting `window.location.href`, even though it only *opens the mail client* — nothing is actually sent, and if no mail client is configured, nothing visible happens yet the UI says sent. Optimistic confirmation. |
| OBS-CONTACT-002 | Contact | Brand is a hardcoded "ARK" wordmark (footer + header) while the app elsewhere brands via trader `business_name`. Public page → static brand; confirm "ARK" is the intended public brand (vs "Kopyya" used in emails/`email_from_name`). |
| OBS-CONTACT-003 | Security | Name/email/message are injected into a `mailto:` via `encodeURIComponent` — this escapes CR/LF, mitigating mailto header injection. Worth an explicit negative test to confirm. |

---

## C. Conventions
- **Test ID:** `LAND-<TYPE>-NNN` / `CONTACT-<TYPE>-NNN`.
- Priority P0–P3 · Severity Critical/High/Medium/Low · Automation: Yes-E2E / Yes-a11y / Yes-perf / Manual.
- Many types are **N/A** for this module (no backend API/DB, no auth on Contact) — marked where so.

---

## D. LANDING (root `/` redirect)

### Functional / Session / Authorization
| ID | Scenario | Pri | Sev | Preconditions | Steps | Expected | Automation |
|---|---|---|---|---|---|---|---|
| LAND-FUNC-001 | Anonymous → login | P0 | High | No token in localStorage | Visit `/` | Redirected to `/login` | Yes-E2E |
| LAND-FUNC-002 | Subscriber → dashboard | P0 | High | Valid subscriber tokens seeded | Visit `/` | Redirected to `/dashboard` | Yes-E2E |
| LAND-FUNC-003 | Trader → dashboard | P1 | High | Valid trader tokens | Visit `/` | Redirected to `/dashboard` | Yes-E2E |
| LAND-FUNC-004 | Admin → admin panel | P1 | High | Valid admin tokens | Visit `/` | Redirected to `/admin` | Yes-E2E |
| LAND-SESS-001 | Stale token cleared | P1 | High | Invalid/expired access token seeded | Visit `/` | `/me` 401 → tokens cleared → `/login` | Yes-E2E |
| LAND-AUTHZ-001 | No content leak pre-redirect | P2 | Medium | No token | Visit `/` | Page renders no authed content (returns null) before redirect | Yes-E2E |

### UI / UX / Performance / Recovery
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| LAND-UI-001 | No content flash | P3 | Low | `/` shows no flash of dashboard/marketing before redirect | Yes-E2E |
| LAND-PERF-001 | Redirect latency | P3 | Low | Redirect resolves quickly (< budget) locally | Yes-perf |
| LAND-RECOV-001 | `/me` network error | P2 | Medium | Backend down / 5xx on `/me` → graceful bounce to `/login`, no hang | Yes-E2E (mock) |

### N/A for Landing
Business-rule, Boundary, Validation, API, DB, Cross-browser-specific, Accessibility (renders no
content — a11y of the *effective* landing `/login` is covered in Sprint 1). Smoke = LAND-FUNC-001.

---

## E. CONTACT (`/contact`)

### Functional / Business Rule
| ID | Scenario | Pri | Sev | Preconditions | Steps | Expected | Automation |
|---|---|---|---|---|---|---|---|
| CONTACT-FUNC-001 | Public load (no auth) | P0 | High | Logged out | Visit `/contact` | 200, page renders form + info cards + footer | Yes-E2E |
| CONTACT-FUNC-002 | Valid submit → mailto + sent | P0 | High | — | Fill name/email/message, Send | mailto navigation triggered; status "sent"; form reset | Yes-E2E |
| CONTACT-BIZ-001 | Endpoint-empty → mailto path | P1 | Medium | `FORM_ENDPOINT=""` | Submit | No network POST; uses `mailto:` fallback | Yes-E2E (assert no request) |
| CONTACT-FUNC-003 | Links resolve | P2 | Low | — | Inspect links | Email/info mailto → `mailto:support@kopyya.com`; Home/Back → `/` | Yes-E2E |

### Validation / Negative / Boundary
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CONTACT-VAL-001 | Empty required fields | P1 | High | Native `required` blocks submit; fields `:invalid`; stays on page | Yes-E2E |
| CONTACT-VAL-002 | Invalid email format | P1 | Medium | `type=email` native validation blocks submit | Yes-E2E |
| CONTACT-BND-001 | Very long message | P3 | Low | Long text accepted (textarea, no maxlength) — no crash | Yes-E2E |
| CONTACT-NEG-001 | Whitespace-only fields | P2 | Low | `required` treats empty; whitespace-only currently passes `required` — record behavior | Yes-E2E |

### UI / UX
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CONTACT-UI-001 | Layout renders | P2 | Medium | Headline, 3 info cards (Email/Response/Account), form title, "* required", footer, "ARK" brand | Yes-E2E |
| CONTACT-UI-002 | Focus styles | P3 | Low | Inputs show accent focus ring/border on focus | Yes-E2E |
| CONTACT-UX-001 | Status slot no layout jump | P3 | Low | Fixed-height status slot; no reflow when message appears | Yes-E2E |
| CONTACT-UX-002 | Optimistic "sent" (OBS-CONTACT-001) | P2 | Medium | Document: "sent" shows after opening mailto even though nothing was truly sent | Manual/E2E |

### Accessibility / Responsive / Cross-browser
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CONTACT-A11Y-001 | axe no serious/critical | P1 | High | Labels associated (Field htmlFor); 0 serious/critical violations, light + dark | Yes-a11y |
| CONTACT-A11Y-002 | Keyboard-only flow | P2 | Medium | Tab through name→email→message→send; Enter submits; visible focus | Manual/E2E |
| CONTACT-RESP-001 | Responsive layout | P2 | Medium | Two-column (lg) collapses to single column at 375/768; no horizontal scroll | Yes-E2E |
| CONTACT-XBROWSER-001 | mailto across engines | P3 | Low | Submit behavior consistent on Chromium/Firefox/WebKit | Yes-E2E |

### Security
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CONTACT-SEC-001 | XSS in fields | P1 | High | `<script>` / `"><img>` in name/message not executed or reflected into DOM (values only go into encoded mailto) | Yes-E2E |
| CONTACT-SEC-002 | mailto header injection (OBS-CONTACT-003) | P1 | High | Name/message with CRLF + `Cc:`/`Bcc:` does not inject mailto headers (encodeURIComponent escapes newlines) | Yes-E2E |
| CONTACT-SEC-003 | No backend attack surface | P2 | Low | No `/api/contact` exists; page is static+client — confirm no server call | Yes-E2E |

### Performance / Smoke / Sanity / Regression / Exploratory
| ID | Scenario | Pri | Sev | Expected | Automation |
|---|---|---|---|---|---|
| CONTACT-PERF-001 | Page load budget | P3 | Low | Local LCP/load within budget (heavy CSS gradients — watch paint) | Yes-perf |
| CONTACT-SMOKE-001 | Loads + form present | P1 | High | `/contact` 200, three fields + Send visible | Yes-E2E |
| CONTACT-RGRS-001 | Suite green vs baseline | P2 | Low | Re-run passes | Yes-E2E |
| CONTACT-EXPL-001 | Charter | P2 | Low | Paste unicode/emoji, huge message, rapid double-submit, mailto with no mail client | Manual |

### N/A for Contact
API/DB (no backend), Session/Authorization (public page), Recovery of a server call (only relevant
if `FORM_ENDPOINT` is later set — noted as a conditional case).

---

## F. Traceability & coverage summary

| Feature | Func | Val/Neg/Bnd | UI/UX | A11y | Resp | Sec | Sess/Authz | Perf | Recov | Smoke | Expl |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Landing | 4 | — | 2 | (login) | — | — | 3 | 1 | 1 | 1 | — |
| Contact | 4 | 4 | 4 | 2 | 1 | 3 | — (public) | 1 | (cond) | 1 | 1 |

**Approx. total: ~32 cases.** Emphasis: Landing = redirect/session correctness; Contact = form
validation, a11y, and the two security tests (XSS + mailto header injection).

## G. Open questions for sign-off
1. **OBS-LAND-001** — confirm "no marketing home; `/` redirects" is intended (so "Home" links → login is acceptable).
2. **OBS-CONTACT-001** — is the optimistic "sent" acceptable, or should the copy say "opening your mail app…"? (Affects expected result / possible Phase-6 fix.)
3. **OBS-CONTACT-002** — public brand: keep "ARK", or align to "Kopyya"? (Consistency; not a functional bug.)
4. Test the `FORM_ENDPOINT`-set (Formspree) path too, or only the shipped mailto path? (Assumed: shipped path only.)

---

*End of Phase 3 — Landing + Contact test design.*
