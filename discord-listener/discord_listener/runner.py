"""Orchestrator: keeps one ChannelWatcher alive per assigned Discord channel.

Reconcile, don't command
------------------------
The backend never pushes "start this watcher" at us. Instead we poll the
assignment list and make reality match it: open what's new, close what's gone,
leave healthy watchers alone. That's the same self-healing shape the broker
listeners use (``services.listeners.run_reconciler``), and it's chosen for the
same reason — a missed message can only delay a watcher by one sweep, where a
command channel would strand it until someone noticed.

One browser, many contexts: each source gets its own isolated BrowserContext, so
one trader's Discord session can never see another's cookies, and a crash in one
channel doesn't take the others down.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from playwright.async_api import async_playwright

from .client import BackendClient
from .config import Config
from .login_worker import LoginWorker
from .watcher import ChannelWatcher

log = logging.getLogger(__name__)

_CHROMIUM_ARGS = [
    # Required in containers: Chromium's sandbox needs privileges this service
    # deliberately does not have. The container itself is the isolation boundary
    # (no DB, no broker credentials, dropped capabilities).
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    # Discord is a heavy SPA; these trim background work in a headless tab.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]


class ListenerRunner:
    def __init__(self, config: Config) -> None:
        self._config = config
        # Playwright handle, kept so login attempts can launch their own
        # short-lived browsers rather than sharing the watchers' long-lived one.
        self._pw: Any = None
        self._client = BackendClient(config)
        self._watchers: dict[str, ChannelWatcher] = {}
        # Short-lived QR login attempts, keyed by session id. Each owns its own
        # throwaway browser context and disposes of itself when it finishes.
        self._logins: dict[str, LoginWorker] = {}
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        async with async_playwright() as pw:
            self._pw = pw
            browser = await pw.chromium.launch(
                headless=self._config.headless, args=_CHROMIUM_ARGS
            )
            log.info(
                "discord listener up (backend=%s, reconcile=%ss)",
                self._config.backend_url, self._config.reconcile_interval_s,
            )
            try:
                await self._reconcile_loop(browser)
            finally:
                await self._shutdown_all()
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    log.debug("browser close failed", exc_info=True)
                await self._client.aclose()
                log.info("discord listener stopped")

    async def _reconcile_loop(self, browser: Any) -> None:
        while not self._shutdown.is_set():
            try:
                await self._reconcile(browser)
            except Exception:  # noqa: BLE001
                # Never let one bad sweep kill the loop — the next one retries.
                log.exception("reconcile sweep failed")
            try:
                await self._reconcile_logins(browser)
            except Exception:  # noqa: BLE001
                log.exception("login sweep failed")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._config.reconcile_interval_s
                )
            except asyncio.TimeoutError:
                continue

    async def _reconcile(self, browser: Any) -> None:
        assignments = await self._client.fetch_assignments()
        if assignments is None:
            # Backend unreachable. Explicitly do NOT treat this as "no
            # assignments" — tearing every watcher down over a blip would drop
            # live channels and replay their backlogs on recovery.
            log.warning("assignments unavailable, keeping %d watcher(s)", len(self._watchers))
            return

        wanted = {str(a["source_id"]): a for a in assignments}

        # Stop watchers whose source was disabled, deleted, or had its session
        # cleared. Also restart any whose channel changed underneath them.
        for source_id in list(self._watchers):
            watcher = self._watchers[source_id]
            assignment = wanted.get(source_id)
            if assignment is None:
                log.info("source=%s unassigned, stopping watcher", source_id)
                await self._stop_watcher(source_id)
            elif not watcher.matches(assignment):
                log.info("source=%s channel changed, restarting watcher", source_id)
                await self._stop_watcher(source_id, report=False)

        for source_id, assignment in wanted.items():
            if source_id in self._watchers:
                continue
            watcher = ChannelWatcher(browser, self._client, self._config, assignment)
            self._watchers[source_id] = watcher
            try:
                await watcher.start()
                log.info("source=%s watcher started (%s)", source_id, watcher.label)
            except Exception as exc:  # noqa: BLE001
                log.exception("source=%s failed to start", source_id)
                self._watchers.pop(source_id, None)
                await self._client.post_status(source_id, "error", error=str(exc))

    async def _reconcile_logins(self, browser: Any) -> None:  # noqa: ARG002
        """Start a worker for each new QR login request; reap finished ones.

        Polled on the same sweep as channel assignments because this container
        has no inbound port — every instruction reaches it by polling.
        """
        for session_id in [s for s, w in self._logins.items() if w.done]:
            self._logins.pop(session_id, None)
            log.info("login=%s finished", session_id)

        for request in await self._client.fetch_login_requests():
            session_id = str(request["session_id"])
            if session_id in self._logins:
                continue
            # Only ONE live login per source. Reopening the dialog creates a new
            # session, and leaving the previous worker running would mean two
            # browsers each showing a different QR for the same source — the
            # trader scans the one on screen while a stale context waits for a
            # code nobody will ever scan.
            source_id = str(request["source_id"])
            for old_id, old_worker in list(self._logins.items()):
                if old_worker.source_id == source_id:
                    log.info("login=%s superseded by %s", old_id, session_id)
                    await old_worker.stop()
                    self._logins.pop(old_id, None)
            worker = LoginWorker(self._pw, self._client, self._config, request)
            self._logins[session_id] = worker
            try:
                await worker.start()
                log.info("login=%s started for source=%s", session_id, request["source_id"])
            except Exception:  # noqa: BLE001
                log.exception("login=%s failed to start", session_id)
                self._logins.pop(session_id, None)

    async def _stop_watcher(self, source_id: str, *, report: bool = True) -> None:
        watcher = self._watchers.pop(source_id, None)
        if watcher is None:
            return
        try:
            await watcher.stop(report=report)
        except Exception:  # noqa: BLE001
            log.exception("source=%s did not stop cleanly", source_id)

    async def _shutdown_all(self) -> None:
        """Graceful shutdown: every watcher drains its observed messages and
        reports 'disconnected', so the UI shows the truth rather than a stale
        'connected' pill until a heartbeat times out."""
        # Cancel in-flight logins first: each holds a browser context with
        # either a live QR or a freshly captured session in it.
        if self._logins:
            log.info("cancelling %d login attempt(s)", len(self._logins))
            await asyncio.gather(
                *(w.stop() for w in self._logins.values()), return_exceptions=True
            )
            self._logins.clear()
        if not self._watchers:
            return
        log.info("stopping %d watcher(s)", len(self._watchers))
        await asyncio.gather(
            *(self._stop_watcher(sid) for sid in list(self._watchers)),
            return_exceptions=True,
        )


async def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runner = ListenerRunner(Config.from_env())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.request_shutdown)
        except NotImplementedError:  # pragma: no cover — Windows dev
            pass

    await runner.run()
