# Copy Trading Platform

One trader, many subscribers. When the trader places an equity or option order, every active subscriber's linked broker accounts mirror it, scaled by their per-tier multiplier.

> **Educational software. Not investment advice.** Copy trading involves substantial risk of loss. The platform operator may need to register as an investment adviser under applicable securities laws (e.g. SEC/FINRA in the US) before charging subscribers. Verify your regulatory obligations before going live.

## Stack

- **Frontend** — Next.js (App Router) + TypeScript + Tailwind
- **Backend** — FastAPI + SQLAlchemy 2.x + Alembic
- **Database** — PostgreSQL 16
- **Auth** — JWT access + refresh
- **Credential storage** — Fernet symmetric encryption (cryptography lib)
- **Brokers** — Alpaca (working), Schwab / E*TRADE / Webull (adapter stubs with documented credential shapes)

## Project layout

```
trading-app/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers
│   │   ├── brokers/      # one adapter per broker, behind a shared interface
│   │   ├── core/         # password hashing, JWT
│   │   ├── models/       # SQLAlchemy ORM
│   │   ├── schemas/      # Pydantic request/response
│   │   ├── services/     # crypto, audit, copy_engine, pnl
│   │   ├── config.py
│   │   ├── database.py
│   │   └── main.py
│   └── alembic/          # migrations
└── frontend/
    ├── app/
    │   ├── login/, register/
    │   └── (app)/        # authenticated pages: brokers, trades, calendar, settings, subscribers, trade-panel
    ├── components/
    └── lib/
```

## Run it locally

### 1. Postgres

```bash
docker compose up -d
```

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Generate real secrets:
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print('CREDENTIAL_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
# Paste both values into .env, replacing the placeholders.

# Generate the initial migration from the models, then apply it:
alembic revision --autogenerate -m "initial schema"
alembic upgrade head

uvicorn app.main:app --reload --port 8000
```

API docs at http://localhost:8000/docs.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000.

## First-run flow

1. Register the **trader** account first (only one is allowed; the API will refuse a second).
2. Register one or more **subscriber** accounts.
3. As the trader, go to **Brokers** → connect an Alpaca paper account (get keys at https://alpaca.markets/), then **Settings** → flip "master trading switch" ON.
4. As a subscriber, go to **Brokers** → connect your own Alpaca paper account, then **Settings** → follow the trader and flip "copy trading" ON.
5. Back as the trader, go to **Trade Panel** → place a small market order. It should fan out to every subscriber whose copy switch is ON, scaled by their multiplier.

## Production notes

- **Secrets**: rotate `JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` only with a migration plan — rotating the encryption key invalidates every stored broker credential.
- **Audit log**: append-only by convention; consider also enforcing this with a Postgres trigger that blocks UPDATE/DELETE on `audit_logs`.
- **Schwab / E*TRADE / Webull**: adapter skeletons exist with the correct credential shapes documented inline. Implement `verify_connection`, `place_order`, `get_order` for each before exposing them in the UI selector (currently only Alpaca is marked `ready: true` on the brokers page).
- **Fills & realized P&L**: the P&L calculator (`services/pnl.py`) reads from the `fills` table. To populate it from real broker activity, add a poller (Alpaca supports order-update streaming via WebSocket) that writes Fill rows when broker orders execute. Without that, calendar P&L stays empty even after orders fill.
- **Regulatory**: before charging anyone, get a US securities lawyer's review. The product structure (a trader monetizing trading signals via automatic order replication) typically requires RIA registration or operating under a regulated copy-trading platform.

## Security checklist (already in place)

- Passwords hashed with bcrypt
- Broker credentials encrypted at rest with Fernet (key in env, never in DB)
- JWT access tokens (short-lived) + refresh tokens
- Append-only `audit_logs` table covering: register, login (success + fail), broker connect/verify/delete, subscriber/trader settings changes, order placed, order rejected, copy fan-out submitted/skipped/error
- CORS restricted to configured origins
- Trader-only routes guarded server-side (`require_trader`); the UI hides them but the API enforces the check
