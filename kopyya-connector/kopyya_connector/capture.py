"""Sign in to Discord in a real browser on the trader's machine and hand the
resulting session to Kopyaa.

This is the whole point of the Connector: the sign-in happens HERE, on the
trader's own computer, in a visible browser, driven by a human. Kopyaa's servers
never load Discord's login page — which is what they were being challenged for.

We do not type, read, or transmit the trader's password or 2FA code. The browser
is opened at Discord's own login page and then left alone until it reaches the
app; the only thing we take is the session cookie state Discord issues
afterwards. If Discord presents a human-verification check, the trader answers it
themselves — it's their browser and they're sitting in front of it. Nothing here
solves, bypasses or evades such a check.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

_LOGIN_URL = "https://discord.com/login"
# Reaching the app shell is what proves the sign-in finished — including any 2FA
# or verification step Discord decided to ask for.
_SIGNED_IN = "**/channels/**"
# Generous: the trader may need to fetch a code from their phone.
_LOGIN_TIMEOUT_MS = 10 * 60 * 1000

# Prefer the trader's installed Chrome. It's a genuine consumer browser rather
# than a bundled automation build, and it avoids shipping ~150MB of Chromium in
# the app. Falls back to the bundled browser when Chrome isn't present.
_CHANNELS = ("chrome", "msedge", None)


class CaptureError(Exception):
    """Something went wrong that the trader can act on."""


def claim_code(backend_url: str, code: str) -> dict[str, Any]:
    """Redeem the pairing code shown in Kopyaa."""
    try:
        resp = httpx.post(
            f"{backend_url.rstrip('/')}/api/discord-sources/pair/claim",
            json={"code": code},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise CaptureError(f"Couldn't reach Kopyaa: {exc}") from exc
    if resp.status_code == 404:
        raise CaptureError(
            "That code isn't valid any more. Codes expire after 10 minutes and "
            "can only be used once — generate a fresh one in Kopyaa."
        )
    if resp.status_code >= 400:
        raise CaptureError(f"Kopyaa rejected the code ({resp.status_code}).")
    return resp.json()


def capture_session(on_status: Callable[[str], None]) -> dict[str, Any]:
    """Open a real browser, wait for the trader to sign in, return the session."""
    last_error: Exception | None = None
    for channel in _CHANNELS:
        try:
            return _capture_with(channel, on_status)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 — browser not installed, etc.
            log.warning("browser channel %s unavailable: %s", channel or "bundled", exc)
            last_error = exc
    raise CaptureError(
        "Couldn't open a browser. Install Google Chrome and try again."
    ) from last_error


def _capture_with(channel: str | None, on_status: Callable[[str], None]) -> dict[str, Any]:
    with sync_playwright() as pw:
        launch: dict[str, Any] = {"headless": False}
        if channel:
            launch["channel"] = channel
        browser = pw.chromium.launch(**launch)
        try:
            context = browser.new_context(viewport={"width": 1180, "height": 860})
            page = context.new_page()
            page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            on_status("Sign in to Discord in the browser window…")

            try:
                page.wait_for_url(_SIGNED_IN, timeout=_LOGIN_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                raise CaptureError(
                    "Timed out waiting for the sign-in. Nothing was saved — "
                    "open the Connector again when you're ready."
                ) from exc

            on_status("Signed in — capturing the session…")
            # Let Discord settle so the session is fully established.
            page.wait_for_timeout(3000)
            state = context.storage_state()
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if not state.get("cookies"):
        raise CaptureError("No session was captured — did the sign-in complete?")
    return state


def upload_session(
    backend_url: str, code: str, upload_token: str, state: dict[str, Any]
) -> None:
    """Hand the session to Kopyaa, which validates and encrypts it."""
    try:
        resp = httpx.post(
            f"{backend_url.rstrip('/')}/api/discord-sources/pair/complete",
            json={"code": code, "upload_token": upload_token, "storage_state": state},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise CaptureError(f"Couldn't reach Kopyaa to finish: {exc}") from exc
    if resp.status_code >= 400:
        raise CaptureError(f"Kopyaa rejected the session ({resp.status_code}).")
