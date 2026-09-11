"""HTTP client for the Kopyaa backend's internal listener endpoints.

Every call carries the shared listener token. Nothing here logs a payload:
assignments contain decrypted Discord sessions, and message content is a
trader's private channel data.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class BackendClient:
    def __init__(self, config: Config) -> None:
        self._base = config.backend_url
        self._client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"X-Kopyaa-Listener-Token": config.listener_token},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_assignments(self) -> list[dict[str, Any]] | None:
        """Channels we should currently have open, or None if the backend was
        unreachable.

        None is deliberately distinct from an empty list: "the backend is down"
        must NOT be read as "close every channel", or a brief blip would tear
        down every watcher and replay backlogs on recovery.
        """
        try:
            resp = await self._client.get(
                f"{self._base}/api/discord-sources/internal/assignments"
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.error(
                "assignments rejected: %s (check KOPYAA_LISTENER_TOKEN)",
                exc.response.status_code,
            )
            return None
        except httpx.HTTPError as exc:
            log.warning("assignments unreachable: %s", exc)
            return None

    async def post_messages(self, source_id: str, messages: list[dict]) -> dict | None:
        """Hand a batch to the backend. Returns the ingest report, or None on
        failure so the caller can decide whether to retry."""
        if not messages:
            return {"accepted": 0, "duplicates": 0, "rejected": []}
        try:
            resp = await self._client.post(
                f"{self._base}/api/discord-sources/internal/messages",
                json={"source_id": source_id, "messages": messages},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # 4xx means this batch is malformed and will never be accepted;
            # logging the count (never the content) is enough to diagnose.
            log.error(
                "message batch rejected for source=%s: %s (%d messages)",
                source_id, exc.response.status_code, len(messages),
            )
            return None
        except httpx.HTTPError as exc:
            log.warning("message batch failed for source=%s: %s", source_id, exc)
            return None

    async def post_status(
        self,
        source_id: str,
        status: str,
        *,
        error: str | None = None,
        channel_name: str | None = None,
        guild_name: str | None = None,
        baseline_message_id: str | None = None,
    ) -> None:
        """Report connection state / heartbeat. Best-effort: a dropped status
        update must never take down the watcher it describes."""
        payload = {"source_id": source_id, "status": status}
        if error:
            payload["error"] = error[:500]
        if channel_name:
            payload["channel_name"] = channel_name
        if guild_name:
            payload["guild_name"] = guild_name
        if baseline_message_id:
            payload["baseline_message_id"] = baseline_message_id
        try:
            resp = await self._client.post(
                f"{self._base}/api/discord-sources/internal/status", json=payload
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("status update failed for source=%s: %s", source_id, exc)

    # ── QR login ────────────────────────────────────────────────────────────

    async def fetch_login_requests(self) -> list[dict[str, Any]]:
        """Login attempts waiting on us. Empty list on failure — unlike channel
        assignments, there's no state to preserve, so a missed poll just means
        the attempt starts one sweep later."""
        try:
            resp = await self._client.get(
                f"{self._base}/api/discord-sources/internal/login-requests"
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("login requests unreachable: %s", exc)
            return []

    async def post_login_qr(self, session_id: str, qr_png_b64: str) -> None:
        """Hand over the current QR frame. Never logged — a live QR is a
        scannable login credential."""
        try:
            resp = await self._client.post(
                f"{self._base}/api/discord-sources/internal/login/{session_id}/qr",
                json={"qr_png": qr_png_b64},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("QR upload failed for login=%s: %s", session_id, exc)

    async def post_login_status(
        self, session_id: str, status: str, *, error: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"status": status}
        if error:
            payload["error"] = error[:500]
        try:
            resp = await self._client.post(
                f"{self._base}/api/discord-sources/internal/login/{session_id}/status",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("login status failed for %s: %s", session_id, exc)

    async def post_login_complete(self, session_id: str, storage_state: dict) -> bool:
        """Hand the captured Discord session to the backend, which validates and
        encrypts it. The state is never written to this container's disk or log."""
        try:
            resp = await self._client.post(
                f"{self._base}/api/discord-sources/internal/login/{session_id}/complete",
                json={"storage_state": storage_state},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("login handover failed for %s: %s", session_id, exc)
            return False
