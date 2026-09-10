"""Runtime configuration, read from the environment.

Kept dependency-free on purpose — this service deliberately does NOT import the
backend's settings module, because it must not be able to reach the database or
broker credentials even by accident.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # Kopyaa backend base URL, e.g. http://backend:8000 inside compose.
    backend_url: str
    # Shared secret for the /api/discord-sources/internal/* endpoints. This is
    # the service's entire authority; without it nothing works.
    listener_token: str
    # How often to reconcile our open channels against the backend's assignment
    # list. Mirrors the broker listeners' 15s reconciler: a self-healing sweep
    # means a missed update delays a watcher rather than stranding it.
    reconcile_interval_s: int = 15
    # Liveness ping per open channel. A quiet alert channel is otherwise
    # indistinguishable from a dead watcher.
    heartbeat_interval_s: int = 30
    # How long the in-page observer batches before flushing. Long enough to
    # coalesce a burst of alerts into one request, short enough to be invisible
    # next to broker latency.
    flush_ms: int = 250
    # Ceiling on consecutive-failure backoff when a channel won't stay open.
    max_backoff_s: int = 300
    headless: bool = True
    # Print each ingested message (author, text, embed title/description/fields)
    # to the log instead of just per-batch counts. On by default because the
    # whole point of this stage is seeing what the channel actually posts —
    # which is also what the trade parser will be built against. Set false in
    # production if you'd rather not have alert content in the log file.
    log_messages: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        backend = (os.environ.get("KOPYAA_BACKEND_URL") or "").rstrip("/")
        token = os.environ.get("KOPYAA_LISTENER_TOKEN") or ""
        if not backend:
            raise SystemExit("KOPYAA_BACKEND_URL is required")
        if not token:
            raise SystemExit("KOPYAA_LISTENER_TOKEN is required")
        return cls(
            backend_url=backend,
            listener_token=token,
            reconcile_interval_s=_int("DISCORD_RECONCILE_INTERVAL_S", 15),
            heartbeat_interval_s=_int("DISCORD_HEARTBEAT_INTERVAL_S", 30),
            flush_ms=_int("DISCORD_FLUSH_MS", 250),
            max_backoff_s=_int("DISCORD_MAX_BACKOFF_S", 300),
            headless=(os.environ.get("DISCORD_HEADLESS", "true").lower() != "false"),
            log_messages=(os.environ.get("DISCORD_LOG_MESSAGES", "true").lower() != "false"),
        )
