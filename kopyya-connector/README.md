# Kopyaa Connector

A small desktop app that connects a trader's Discord account to Kopyaa.

## Why it exists

Kopyaa monitors a Discord channel by keeping an authenticated browser session
open. Capturing that session **server-side** means an automated browser sitting
on Discord's login page — which Discord challenges with a CAPTCHA on its own
schedule (we measured 20–60 seconds, unpredictably). We do not solve or evade
such challenges, so server-side login can't be the onboarding path.

The Connector moves the sign-in to where it belongs: the trader's own computer,
in a real browser, driven by a human. If Discord does ask for verification, the
trader answers it themselves — they're sitting right there.

## What it does and doesn't do

* Opens **Discord's own login page** in a real browser and waits.
* Captures only the **session** Discord issues after a successful sign-in.
* Sends that session to Kopyaa, which encrypts it (Fernet) before storage.

It never reads, stores, or transmits a password or 2FA code, and it never
attempts to bypass Discord authentication, permissions, or verification checks.

## Trader flow

```
Kopyaa  →  Add channel  →  Connect Discord  →  shows KPY-4F2A-9C1D
Connector  →  paste code  →  Connect  →  browser opens  →  sign in
Kopyaa  →  Connected
```

## Pairing codes

The Connector has no Kopyaa login. A pairing code tells it which source to
attach to and proves it's allowed to:

* **Single use** — claiming it once makes it useless to anyone else
* **10 minute TTL**
* **Shown only to the signed-in owner** of that source
* Claiming returns an `upload_token`, which is what actually authorises the
  write. The code alone can't store a session.

## Running from source

```bash
pip install -r requirements.txt && playwright install chromium
python -m kopyaa_connector
```

Point it at a non-production backend with `KOPYAA_BACKEND_URL`.

## Browser choice

Prefers the trader's installed **Chrome**, then **Edge**, then Playwright's
bundled Chromium. A real consumer browser is both less likely to be challenged
and avoids shipping ~150MB of Chromium in the app.

## Packaging (not done yet)

`pyinstaller --windowed --name "Kopyaa Connector" kopyaa_connector/__main__.py`,
then code-sign and notarise for macOS and sign for Windows. Both need your
developer certificates — unsigned builds are blocked by Gatekeeper and
SmartScreen.
