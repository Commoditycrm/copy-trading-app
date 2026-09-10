"""Drives one QR login attempt in a throwaway browser context.

    open discord.com/login  ->  screenshot the QR  ->  trader scans on their phone
        ->  Discord authenticates the browser  ->  capture storage_state  ->  hand over

We never type anything into Discord. The QR is rendered by Discord's own login
page, and the trader approves the sign-in on their phone, so no password or MFA
code exists anywhere in this process. Nothing about Discord's authentication is
bypassed — this is its own documented login flow, driven by the account holder.

The context is created fresh with NO storage state and closed when the attempt
ends, so a login attempt can never inherit or leak another account's session.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from .client import BackendClient
from .config import Config

log = logging.getLogger(__name__)

_LOGIN_URL = "https://discord.com/login"
# Reaching the app shell is what proves the sign-in completed.
_SIGNED_IN_PATH = "/channels/"

# Discord's login QR. Class names in the bundle are hashed, so match on the
# stable substring.
#
# A bare "canvas" fallback used to live here and was actively harmful: when no QR
# was on the page it matched whatever else was drawn — including Discord's
# anti-bot challenge — and we served a screenshot of THAT to the trader as if it
# were a scannable code. A selector that always finds something is worse than one
# that finds nothing, because "no QR" is real information the trader needs.
# Minimal launch args. The listener's shared browser adds several
# --disable-* flags for long-running headless efficiency; a login browser lives
# for seconds, so it stays as close to a stock Chromium as possible.
_LOGIN_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_QR_SELECTORS = (
    '[class*="qrCodeContainer"]',
    '[class*="qrCode"]',
    'div[class*="qrLogin"] canvas',
    'div[class*="qrLogin"] img',
)

# Discord's bot-detection challenge. We DETECT it and stop; we never attempt to
# solve, bypass or evade it. Automating past a CAPTCHA is both off-limits and the
# quickest way to get a trader's Discord account terminated. Detection exists so
# the trader is told what happened and pointed at the manual fallback.
_CHALLENGE_SELECTORS = (
    'iframe[src*="hcaptcha"]',
    'iframe[src*="recaptcha"]',
    '[class*="captcha"]',
)
_CHALLENGE_TEXT = ("are you human", "confirm you're not a robot", "verify you are human")

# How often to re-capture the QR. Discord rotates it roughly every 2 minutes, so
# this stays comfortably inside that while being far gentler than the 3s loop
# this replaced: screenshotting someone else's login page twenty times a minute
# is both rude and a machine-regular interaction pattern that behavioural
# anti-bot scoring is built to notice.
_CAPTURE_INTERVAL_S = 30.0
# How often to check the page URL. Cheap (no DOM serialisation), so it can be
# frequent — this is what makes a completed sign-in feel instant even though the
# QR itself is only re-captured every _CAPTURE_INTERVAL_S.
_POLL_INTERVAL_S = 2.0
# Whole-attempt ceiling. Comfortably longer than "find phone, unlock, scan",
# short enough that an abandoned attempt doesn't hold a browser context open.
_ATTEMPT_TIMEOUT_S = 300


class LoginWorker:
    """Runs one login attempt to completion, then disposes of its context."""

    def __init__(
        self,
        playwright: Any,
        client: BackendClient,
        config: Config,
        request: dict[str, Any],
    ) -> None:
        # A playwright handle, NOT the shared browser: each attempt launches and
        # disposes of its own Chromium. Two reasons. Isolation — one trader's
        # sign-in must not share a browser process with another's, or with the
        # long-lived watcher browser holding live Discord sessions. And parity —
        # a freshly launched browser is the exact configuration that loads
        # Discord's login page cleanly in testing, whereas the shared instance
        # did not.
        self._pw = playwright
        self._client = client
        self._config = config
        self.session_id: str = str(request["session_id"])
        self.source_id: str = str(request["source_id"])
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"login:{self.session_id}")

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        browser = context = page = None
        try:
            await self._client.post_login_status(self.session_id, "starting")
            browser = await self._pw.chromium.launch(
                headless=self._config.headless, args=_LOGIN_BROWSER_ARGS
            )
            # Deliberately NO storage_state: a login attempt must start signed
            # out, or Discord would skip the QR and silently re-use whatever
            # account was already authenticated.
            context = await browser.new_context(viewport={"width": 1100, "height": 820})
            page = await context.new_page()
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            captured = await self._pump_qr(page)
            if not captured:
                # Keep evidence of what the page actually looked like — a bare
                # "timed out" tells us nothing about whether the QR rendered,
                # the scan registered, or Discord asked for something else.
                shot = await self._debug_shot(page)
                log.warning(
                    "login=%s timed out at url=%s (screenshot: %s)",
                    self.session_id, page.url, shot or "unavailable",
                )
                await self._client.post_login_status(
                    self.session_id, "failed",
                    error="Timed out waiting for the sign-in to complete. Try again.",
                )
                return

            state = await context.storage_state()
            if not state.get("cookies"):
                await self._client.post_login_status(
                    self.session_id, "failed", error="No session was captured — please retry."
                )
                return

            await self._client.post_login_complete(self.session_id, state)
            log.info("login=%s captured a Discord session for source=%s",
                     self.session_id, self.source_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("login=%s failed", self.session_id)
            await self._client.post_login_status(self.session_id, "failed", error=str(exc)[:400])
        finally:
            # Always tear the whole browser down: it holds either a live QR or a
            # real Discord session, and neither should outlive the attempt.
            for closer in (page, context, browser):
                if closer is not None:
                    try:
                        await closer.close()
                    except Exception:  # noqa: BLE001
                        log.debug("login=%s cleanup failed", self.session_id, exc_info=True)

    async def _pump_qr(self, page: Any) -> bool:
        """Stream QR frames until the trader signs in. True once signed in.

        Two cadences on purpose: the URL is checked every couple of seconds
        (cheap, and it's what makes a completed sign-in register promptly), while
        the QR is only re-captured every ``_CAPTURE_INTERVAL_S``. The previous
        version screenshotted and re-read the whole document body every 3
        seconds, which is far more interaction with Discord's login page than
        this needs.
        """
        elapsed = 0.0
        since_capture = _CAPTURE_INTERVAL_S   # capture immediately on entry
        sent_any = False
        announced_scan = False
        last_url = ""
        checked_challenge = False

        while elapsed < _ATTEMPT_TIMEOUT_S:
            url = page.url or ""
            if url != last_url:
                log.info("login=%s url -> %s", self.session_id, url)
                last_url = url

            if _SIGNED_IN_PATH in url:
                # Let Discord finish establishing the session before capture.
                await page.wait_for_timeout(3000)
                return True

            if since_capture >= _CAPTURE_INTERVAL_S:
                since_capture = 0.0

                # Check for a challenge only alongside a capture, not every
                # tick — reading the whole body repeatedly is exactly the kind
                # of machine-regular page interaction we want to avoid.
                if await self._challenged(page):
                    shot = await self._debug_shot(page)
                    log.warning(
                        "login=%s Discord served a bot-detection challenge at %s "
                        "(screenshot: %s) — not attempting to solve it",
                        self.session_id, url, shot or "unavailable",
                    )
                    await self._client.post_login_status(
                        self.session_id, "failed",
                        error=(
                            "Discord asked our browser to complete a human-verification "
                            "check, which we don't attempt to bypass. Wait a few minutes "
                            "and try again."
                        ),
                    )
                    return False
                checked_challenge = True

                element = await self._find_qr(page)
                if element is not None:
                    try:
                        png = await element.screenshot(type="png", timeout=10_000)
                        await self._client.post_login_qr(
                            self.session_id, base64.b64encode(png).decode()
                        )
                        sent_any = True
                    except Exception:  # noqa: BLE001
                        log.debug("login=%s QR capture failed", self.session_id, exc_info=True)
                elif sent_any and not announced_scan:
                    # The QR vanished after we'd shown one: Discord replaces it
                    # with a confirmation prompt the moment a phone scans it.
                    announced_scan = True
                    await self._client.post_login_status(self.session_id, "scanned")

            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
            since_capture += _POLL_INTERVAL_S

        return _SIGNED_IN_PATH in (page.url or "")

    async def _challenged(self, page: Any) -> bool:
        """Is Discord showing an anti-bot challenge instead of the login form?

        Detection only. We report it and stop — solving or evading it is out of
        bounds, and a trader is far better served by an honest "Discord blocked
        the automated sign-in" than by a silent five-minute timeout.
        """
        for selector in _CHALLENGE_SELECTORS:
            try:
                element = await page.query_selector(selector)
                if element is not None and await element.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:  # noqa: BLE001
            return False
        return any(phrase in body for phrase in _CHALLENGE_TEXT)

    async def _debug_shot(self, page: Any) -> str | None:
        """Full-page screenshot for diagnosing a failed attempt. Written under
        /tmp (the container's noexec scratch) and overwritten per session."""
        path = f"/tmp/discord-login-{self.session_id}.png"
        try:
            await page.screenshot(path=path, full_page=True)
            return path
        except Exception:  # noqa: BLE001
            return None

    async def _find_qr(self, page: Any) -> Any | None:
        for selector in _QR_SELECTORS:
            try:
                element = await page.query_selector(selector)
                if element is not None and await element.is_visible():
                    return element
            except Exception:  # noqa: BLE001
                continue
        return None
