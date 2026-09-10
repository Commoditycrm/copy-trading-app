# Kopyaa Discord listener

Real-time trade-alert detection from Discord Web (STEP 2 of inbound
alert-copying). Watches the channels a trader has connected and forwards newly
rendered messages to the Kopyaa backend.

This service **only detects and forwards messages**. Parsing trades, validating
signals, risk checks and order placement are later steps and live in the
backend — a message coming out of here is data, never an order.

## How it reads Discord

It opens Discord Web in a headless Chromium using a session the trader
established themselves, and attaches a `MutationObserver` to the message list.
New messages are reported the moment React renders them — no polling, no DOM
re-scanning on a timer, and no calls to Discord's API.

It reads only what that account can already legitimately see. No Discord
authentication, permission, MFA or CAPTCHA mechanism is bypassed, and the
service never attempts to log in: if a session expires, the source is flagged
`needs_login` and the trader signs in again themselves.

## Isolation

By design this container has **no database connection and no broker
credentials**. Its only authority is `KOPYAA_LISTENER_TOKEN`, which lets it:

* fetch the channel assignments it should watch (including the Discord sessions
  needed to open them), and
* post observed messages and heartbeats back.

Sessions are held in memory for the life of a browser context — never written to
disk, never logged.

## One-time Discord sign-in

Run on the trader's own machine, where a browser window can be shown:

```bash
pip install -r requirements.txt && playwright install chromium
python -m discord_listener.login --out discord-session.json
```

A real Discord login page opens. The trader signs in — password and MFA typed by
them, into Discord. The resulting session is written to
`discord-session.json`; upload it in Kopyaa → Discord, then delete the local
file. Kopyaa stores it Fernet-encrypted, like a broker credential.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `KOPYAA_BACKEND_URL` | yes | — | Backend base URL, e.g. `http://backend:8000` |
| `KOPYAA_LISTENER_TOKEN` | yes | — | Shared secret for the internal endpoints |
| `DISCORD_RECONCILE_INTERVAL_S` | no | `15` | Assignment reconcile sweep |
| `DISCORD_HEARTBEAT_INTERVAL_S` | no | `30` | Liveness ping per channel |
| `DISCORD_FLUSH_MS` | no | `250` | Observer batch window |
| `DISCORD_MAX_BACKOFF_S` | no | `300` | Reconnect backoff ceiling |
| `DISCORD_HEADLESS` | no | `true` | Set `false` to watch it work locally |

The backend must have `DISCORD_LISTENER_ENABLED=true` and the same
`DISCORD_LISTENER_TOKEN`, or every internal endpoint returns 503.

## Resource notes

Each watched channel is its own browser context — roughly 150–250 MB with images,
video and fonts blocked. Size the container's memory limit against the number of
channels being watched.
