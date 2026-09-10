"""One-time Discord sign-in helper — run this on YOUR OWN machine.

    python -m discord_listener.login --out discord-session.json

Opens a real, visible Chromium window at Discord's own login page and waits for
YOU to sign in. Your password and your MFA code are typed by you, into Discord,
in that window. This tool does not read them, does not store them, and does not
transmit them anywhere. Nothing here automates or bypasses Discord's
authentication, MFA, or CAPTCHA — if Discord challenges you, you answer it, the
same as any other login.

What it captures, once you are signed in, is the resulting browser session
(Playwright's ``storage_state``). That session is what lets Kopyaa's listener
open the channels your account can already legitimately read. Treat the output
file like a password: it grants access to your Discord account until you log out
or the session expires. Upload it to Kopyaa, then delete the local copy.

Two ways to deliver it:

  1. Upload the file in Kopyaa → Discord → the source's "Connect Discord" step
     (recommended — your browser session does the authenticating).
  2. Pass --source-id and set KOPYAA_API_TOKEN to have this tool upload it for
     you. The token goes in the environment, not on the command line, so it
     doesn't land in your shell history or the process list.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

log = logging.getLogger("discord_listener.login")

_LOGIN_URL = "https://discord.com/login"
# Reaching the app shell is what proves the sign-in completed — including any
# MFA or CAPTCHA step Discord decided to ask for.
_SIGNED_IN = "**/channels/**"
# Generous: the user may need to fetch a code from their phone or authenticator.
_LOGIN_TIMEOUT_MS = 10 * 60 * 1000


async def capture(out_path: Path, *, keep_open: bool) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1180, "height": 860})
        page = await context.new_page()
        await page.goto(_LOGIN_URL, wait_until="domcontentloaded")

        print()
        print("  A browser window has opened at Discord's login page.")
        print("  Sign in there — including any 2FA prompt. Nothing you type is")
        print("  visible to this tool.")
        print()
        print("  Waiting for you to finish signing in...")

        try:
            await page.wait_for_url(_SIGNED_IN, timeout=_LOGIN_TIMEOUT_MS)
        except Exception:
            await browser.close()
            raise SystemExit(
                "Timed out waiting for sign-in. Nothing was saved — re-run when ready."
            )

        # Let Discord settle so the session is fully established before capture.
        await page.wait_for_timeout(3000)
        state = await context.storage_state()

        if keep_open:
            print("  Signed in. Press Enter here to close the browser and save...")
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)

        await browser.close()

    if not state.get("cookies"):
        raise SystemExit("No session was captured — did the sign-in complete?")

    out_path.write_text(json.dumps(state))
    # Owner-only: this file is a live credential for the Discord account.
    try:
        out_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        log.warning("could not restrict permissions on %s — do so manually", out_path)
    return state


def upload(state: dict, backend_url: str, source_id: str, api_token: str) -> None:
    """PUT the captured session to Kopyaa as the signed-in trader."""
    resp = httpx.put(
        f"{backend_url.rstrip('/')}/api/discord-sources/{source_id}/session",
        json={"storage_state": state},
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Upload failed ({resp.status_code}): {resp.text[:300]}")
    print(f"  Uploaded to Kopyaa. Source {source_id} is ready to connect.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture a Discord Web session for Kopyaa's alert listener.",
    )
    parser.add_argument(
        "--out", default="discord-session.json", help="where to write the session file"
    )
    parser.add_argument(
        "--source-id", help="Kopyaa Discord source id to upload the session to"
    )
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("KOPYAA_BACKEND_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="pause before closing the browser (useful if Discord is still loading)",
    )
    args = parser.parse_args()

    out_path = Path(args.out).expanduser().resolve()
    state = asyncio.run(capture(out_path, keep_open=args.keep_open))
    print(f"  Session saved to {out_path}")

    if args.source_id:
        token = os.environ.get("KOPYAA_API_TOKEN", "")
        if not token:
            raise SystemExit(
                "Set KOPYAA_API_TOKEN (your Kopyaa access token) to upload, or "
                "upload the file through the Kopyaa web UI instead."
            )
        upload(state, args.backend_url, args.source_id, token)
        print()
        print("  Delete the local session file now — Kopyaa has an encrypted copy:")
        print(f"    rm {out_path}")
    else:
        print()
        print("  Next: upload this file in Kopyaa → Discord, then delete it.")


if __name__ == "__main__":
    main()
