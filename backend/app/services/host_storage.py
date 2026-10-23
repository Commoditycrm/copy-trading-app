"""Host disk usage by category, for Admin → System → Storage.

scripts/host_storage.py measures the disk on the host every 15 minutes and
writes storage.json and storage-history.json into the read-only /host-metrics
mount; this only reads them. No file means the collector isn't installed
(production, or QA before the install) — "unavailable", never zeros.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

# The collector runs every 15 min; an hour without a fresh file is stale.
STALE_AFTER_S = 3600


def _dir() -> str:
    return os.environ.get("HOST_METRICS_DIR", "/host-metrics")


def _read(name: str) -> Any:
    with open(os.path.join(_dir(), name)) as f:
        return json.load(f)


def read_storage(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        data = _read("storage.json")
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "The storage collector isn't installed on this server.",
                "action_needed": "Install kopyya-host-storage on the QA host."}
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"storage file unreadable ({type(exc).__name__})",
                "action_needed": "Check `journalctl -u kopyya-host-storage` on the host."}
    if not isinstance(data, dict) or "filesystem" not in data or "categories" not in data:
        return {"status": "unavailable", "reason": "storage file is malformed",
                "action_needed": "Check `journalctl -u kopyya-host-storage` on the host."}
    try:
        age = (now - datetime.fromisoformat(data["collected_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    try:
        history = _read("storage-history.json")
        if not isinstance(history, list):
            history = []
    except (OSError, ValueError):
        history = []
    return {
        "status": "ok",
        **data,
        "history": history[-90:],
        "age_seconds": None if age is None else max(0, int(age)),
        "stale": age is None or age > STALE_AFTER_S,
    }
