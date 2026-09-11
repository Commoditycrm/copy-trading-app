"""One watched Discord channel: a browser context, a page, and an observer.

Lifecycle
---------
    start() → context(storage_state) → goto(channel) → verify session
        → expose __kopyaaEmit → inject observer.js → supervise loop
                                                  ↘ message queue → backend

The supervise loop is what makes this survive real life. Discord Web reloads
itself, navigates on its own, and drops the message list during virtualisation;
a watcher that only checked at startup would report "connected" forever while
seeing nothing. Every tick we re-verify the page is still on our channel and the
observer is still attached, and re-establish whatever is missing.

Session handling: the storage state arrives from the backend already decrypted,
is passed straight to Playwright, and is never written to disk or logged. If
Discord no longer accepts it, we report ``needs_login`` and stop rather than
retrying — a dead session cannot be revived by trying harder, and hammering the
login page would be exactly the kind of behaviour we don't want.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

from .client import BackendClient
from .config import Config

log = logging.getLogger(__name__)

_OBSERVER_JS = (Path(__file__).parent / "observer.js").read_text()

_DISCORD_APP = "https://discord.com/channels"
# Discord bounces an unauthenticated session to /login; it also parks a valid
# session with no channel access on the friends view (/channels/@me).
_LOGIN_MARKERS = ("/login", "/register")


class ChannelWatcher:
    """Watches exactly one channel for one source."""

    def __init__(
        self,
        browser: Any,
        client: BackendClient,
        config: Config,
        assignment: dict[str, Any],
    ) -> None:
        self._browser = browser
        self._client = client
        self._config = config

        self.source_id: str = str(assignment["source_id"])
        self.channel_id: str = str(assignment["channel_id"])
        self.guild_id: str = str(assignment.get("guild_id") or "@me")
        self.label: str = assignment.get("label") or self.channel_id
        self._last_seen: str | None = assignment.get("last_seen_message_id")
        # Held in memory only, for the life of the context.
        self._storage_state: dict = assignment["storage_state"]

        self._context: Any = None
        self._page: Any = None
        self._queue: asyncio.Queue[list[dict]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._failures = 0

    # ── identity ────────────────────────────────────────────────────────────

    @property
    def channel_url(self) -> str:
        return f"{_DISCORD_APP}/{self.guild_id}/{self.channel_id}"

    def matches(self, assignment: dict[str, Any]) -> bool:
        """True if an incoming assignment describes the same channel this
        watcher already has open — used by the reconciler to leave healthy
        watchers alone instead of restarting them every sweep."""
        return (
            str(assignment["source_id"]) == self.source_id
            and str(assignment["channel_id"]) == self.channel_id
            and str(assignment.get("guild_id") or "@me") == self.guild_id
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._sender_loop(), name=f"send:{self.source_id}"),
            asyncio.create_task(self._run(), name=f"watch:{self.source_id}"),
        ]

    async def stop(self, *, report: bool = True) -> None:
        """Graceful shutdown: stop supervising, drain what we already observed,
        then tear the browser context down.

        Draining first matters — messages sitting in the queue were genuinely
        seen, and dropping them on a routine restart would silently lose alerts.
        """
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        await self._drain_queue()
        await self._close_context()
        if report:
            await self._client.post_status(self.source_id, "disconnected")

    async def _close_context(self) -> None:
        for closer in (self._page, self._context):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:  # noqa: BLE001
                log.debug("close failed for source=%s", self.source_id, exc_info=True)
        self._page = None
        self._context = None

    # ── main loop ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Connect, supervise, and reconnect with backoff until stopped."""
        while not self._stopping.is_set():
            try:
                await self._client.post_status(self.source_id, "connecting")
                await self._connect()
                self._failures = 0
                await self._supervise()
            except asyncio.CancelledError:
                raise
            except _SessionExpired as exc:
                # Terminal without trader action: report and stop retrying.
                log.warning("source=%s session no longer valid", self.source_id)
                await self._client.post_status(
                    self.source_id, "needs_login", error=str(exc)
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._failures += 1
                delay = self._backoff()
                log.warning(
                    "source=%s watcher failed (attempt %d), retrying in %.0fs: %s",
                    self.source_id, self._failures, delay, exc,
                )
                await self._client.post_status(self.source_id, "error", error=str(exc))
                await self._close_context()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    return  # stop requested during the backoff
                except asyncio.TimeoutError:
                    continue

    def _backoff(self) -> float:
        """Exponential backoff with jitter, capped. Jitter matters because every
        watcher fails at once when the network drops — without it they would all
        reconnect in lockstep."""
        base = min(self._config.max_backoff_s, 2 ** min(self._failures, 8))
        return base * (0.5 + random.random() / 2)

    async def _connect(self) -> None:
        await self._close_context()
        self._context = await self._browser.new_context(
            storage_state=self._storage_state,
            viewport={"width": 1280, "height": 900},
            # Discord renders a reduced client to unknown agents; leave the
            # bundled Chromium's own UA alone rather than spoofing one.
        )
        self._page = await self._context.new_page()
        # Images/fonts/video are pure weight for a text observer, and dropping
        # them cuts a Discord tab's memory footprint substantially — the
        # difference between a few watchers fitting on the box and not.
        await self._page.route("**/*", _block_heavy_assets)
        await self._page.expose_function("__kopyaaEmit", self._on_emit)

        await self._page.goto(self.channel_url, wait_until="domcontentloaded", timeout=60_000)
        await self._verify_session()
        await self._await_channel()
        await self._inject_observer()

        await self._set_baseline()

        names = await self._read_names()
        await self._client.post_status(
            self.source_id,
            "connected",
            channel_name=names.get("channel"),
            guild_name=names.get("guild"),
        )
        log.info("source=%s watching #%s", self.source_id, names.get("channel") or self.channel_id)

    async def _verify_session(self) -> None:
        """Confirm Discord still accepts the stored session.

        We never attempt to log in: if the session is dead, only the trader can
        fix it by signing in again themselves.
        """
        url = self._page.url or ""
        if any(marker in url for marker in _LOGIN_MARKERS):
            raise _SessionExpired(
                "Discord signed this session out. Run the login helper again to reconnect."
            )

    async def _await_channel(self) -> None:
        """Wait for the message list to render, which is what proves the account
        can actually open this channel."""
        try:
            await self._page.wait_for_selector(
                '[data-list-id="chat-messages"]', timeout=45_000
            )
        except Exception as exc:  # noqa: BLE001
            await self._verify_session()  # a mid-load bounce to /login
            if f"/{self.channel_id}" not in (self._page.url or ""):
                raise _ChannelUnavailable(
                    "Discord redirected away from that channel — the account may no "
                    "longer have access to it."
                ) from exc
            raise _ChannelUnavailable(
                "The channel didn't finish loading. It may have been deleted, or "
                "this account may not have permission to view it."
            ) from exc

    async def _inject_observer(self) -> None:
        await self._page.evaluate(
            _OBSERVER_JS,
            {
                "channelId": self.channel_id,
                "lastSeenMessageId": self._last_seen,
                "flushMs": self._config.flush_ms,
            },
        )

    async def _set_baseline(self) -> None:
        """On a channel's FIRST attach, record where watching started.

        Connecting a channel means "watch it from here", not "import its
        history" — so the observer emits none of the rendered backlog. But the
        starting point has to be persisted, or the next reconnect would suppress
        the backlog again and lose anything posted while we were away.

        Only ever set when the channel has no mark yet; it is never moved
        backwards.
        """
        if self._last_seen:
            return
        try:
            baseline = await self._page.evaluate(
                "() => (window.__kopyaaBaseline ? window.__kopyaaBaseline() : null)"
            )
        except Exception:  # noqa: BLE001
            log.debug("source=%s baseline unavailable", self.source_id, exc_info=True)
            return
        if not baseline:
            return
        self._last_seen = str(baseline)
        await self._client.post_status(
            self.source_id, "connected", baseline_message_id=self._last_seen
        )
        log.info(
            "source=%s watching from message %s onward (existing history skipped)",
            self.source_id, self._last_seen,
        )

    async def _read_names(self) -> dict[str, str | None]:
        """Best-effort display names for the UI. Never fatal — a missing name is
        cosmetic, and failing the connection over it would be absurd."""
        try:
            return await self._page.evaluate(
                r"""() => {
                    /* Read the names out of document.title, which Discord keeps
                       as "<bullet> Discord | #channel-name | Server Name" (the
                       leading bullet or "(3)" appears when there are unreads).
                       Every DOM alternative depends on hashed class names that
                       shift between client builds, and the sidebar variants
                       happily return the server header and channel glued
                       together. The title is stable and already formatted.

                       Split from the RIGHT: the constant "Discord" prefix sits
                       on the left and a server name may itself contain "|",
                       whereas Discord forbids "|" in channel names — so the last
                       two segments are always (channel, server). */
                    const clean = (v) => {
                        if (!v) return null;
                        const s = String(v).replace(/\s+/g, ' ').replace(/^#/, '').trim().slice(0, 200);
                        return s || null;
                    };
                    const raw = (document.title || '')
                        .replace(/^[\u2022\s]*/, '')      /* unread bullet */
                        .replace(/^\(\d+\)\s*/, '');     /* unread count */
                    const parts = raw.split('|').map((p) => p.trim()).filter(Boolean);
                    if (parts.length >= 3) {
                        return { channel: clean(parts[parts.length - 2]),
                                 guild: clean(parts[parts.length - 1]) };
                    }
                    /* DMs and group DMs have no server half. */
                    if (parts.length === 2) return { channel: clean(parts[1]), guild: null };
                    return { channel: null, guild: null };
                }"""
            )
        except Exception:  # noqa: BLE001
            return {"channel": None, "guild": None}

    # ── supervision ─────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Heartbeat + self-heal until something breaks or we're told to stop.

        Raising from here sends us back to ``_run``'s reconnect path, which is
        exactly what we want for anything a re-navigation can't fix.
        """
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._config.heartbeat_interval_s
                )
                return
            except asyncio.TimeoutError:
                pass

            if self._page is None or self._page.is_closed():
                raise _WatcherLost("browser page closed")

            # Discord navigated us elsewhere (client reload, channel deleted,
            # session bounce). Go back before the observer's context is gone.
            if f"/{self.channel_id}" not in (self._page.url or ""):
                log.info("source=%s drifted off channel, re-navigating", self.source_id)
                await self._page.goto(
                    self.channel_url, wait_until="domcontentloaded", timeout=60_000
                )
                await self._verify_session()
                await self._await_channel()

            health = await self._page.evaluate(
                "() => (window.__kopyaaHealth ? window.__kopyaaHealth() : null)"
            )
            if not health or not health.get("attached"):
                # A full client re-render tears the observer out with the old
                # document; re-injecting is the normal repair, not an error.
                log.info("source=%s observer detached, re-injecting", self.source_id)
                await self._inject_observer()

            await self._client.post_status(self.source_id, "connected")

    # ── message plumbing ────────────────────────────────────────────────────

    async def _on_emit(self, payload: str) -> None:
        """Called from the page whenever the observer flushes a batch.

        Deliberately does no I/O: this runs on Playwright's callback path, and
        blocking it would stall the page. Parse, enqueue, return.
        """
        try:
            batch = json.loads(payload)
        except (ValueError, TypeError):
            log.warning("source=%s emitted an unparseable batch", self.source_id)
            return
        if isinstance(batch, list) and batch:
            self._queue.put_nowait(batch)

    async def _sender_loop(self) -> None:
        while True:
            batch = await self._queue.get()
            await self._send(batch)

    async def _send(self, batch: list[dict]) -> None:
        report = await self._client.post_messages(self.source_id, batch)
        if report is None:
            # Backend unreachable. The observer has already marked these as
            # emitted, so re-queue rather than lose them; a reconnect would
            # replay from last_seen, but only for the still-rendered backlog.
            log.warning(
                "source=%s could not deliver %d message(s), re-queueing",
                self.source_id, len(batch),
            )
            await asyncio.sleep(2)
            self._queue.put_nowait(batch)
            return
        if report.get("accepted"):
            newest = max((m["message_id"] for m in batch), key=int, default=None)
            if newest and (self._last_seen is None or int(newest) > int(self._last_seen)):
                self._last_seen = newest
        log.info(
            "source=%s ingested accepted=%s duplicates=%s",
            self.source_id, report.get("accepted"), report.get("duplicates"),
        )
        if self._config.log_messages and report.get("accepted"):
            self._log_messages(batch)

    def _log_messages(self, batch: list[dict]) -> None:
        """Print what was actually observed, not just how many.

        These alerts carry everything in embeds rather than message text, so a
        count alone tells you nothing about whether extraction worked. Rendering
        the fields here is what makes a missing strike or a mangled expiry
        obvious now, instead of surfacing later as a bad trade signal.
        """
        for m in batch:
            when = str(m.get("timestamp") or "")[:19]
            log.info("  ┌─ %s  %s", when, m.get("author") or "(system)")
            content = (m.get("content") or "").strip()
            if content:
                for line in content.splitlines()[:6]:
                    log.info("  │  %s", line[:200])
            for e in m.get("embeds") or []:
                if e.get("title"):
                    log.info("  │  « %s »", e["title"])
                for line in (e.get("description") or "").splitlines():
                    if line.strip():
                        log.info("  │    %s", line[:200])
                for f in e.get("fields") or []:
                    log.info("  │    %s: %s", f.get("name"), f.get("value"))
                if e.get("footer"):
                    log.info("  │    (%s)", e["footer"][:160])
            for a in m.get("attachments") or []:
                log.info("  │  [attachment] %s", a.get("filename") or a.get("url"))
            log.info("  └─ id=%s%s", m.get("message_id"), " (edited)" if m.get("is_edit") else "")

    async def _drain_queue(self) -> None:
        """Flush anything already observed before we shut the context down."""
        pending: list[dict] = []
        while not self._queue.empty():
            pending.extend(self._queue.get_nowait())
        if pending:
            await self._client.post_messages(self.source_id, pending)


async def _block_heavy_assets(route: Any) -> None:
    """Drop images, video and fonts before they're fetched.

    A text observer needs none of them, and a Discord tab that renders no media
    uses a fraction of the memory — which is what decides how many channels fit
    on one box. Failures fall through to continue_ so a routing hiccup can never
    stall the page.
    """
    try:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    except Exception:  # noqa: BLE001
        log.debug("asset routing failed", exc_info=True)


class _SessionExpired(Exception):
    """Discord no longer accepts the stored session; only the trader can fix it."""


class _ChannelUnavailable(Exception):
    """The channel didn't load — deleted, or not visible to this account."""


class _WatcherLost(Exception):
    """The browser page went away underneath us."""
