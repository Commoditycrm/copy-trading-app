"""AWS cost of the QA (EC2) environment, for Admin → System.

The backend never talks to AWS: scripts/aws_costs.py runs on the QA host with
the instance role and writes aws-costs.json into the read-only /host-metrics
mount. No collector file (production, or QA before it's installed) means
"unavailable", never $0.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

# The collector runs every 8h; allow one missed run before calling it stale.
STALE_AFTER_S = 18 * 3600


def _path() -> str:
    return os.environ.get("AWS_COSTS_FILE", "/host-metrics/aws-costs.json")


def _unavailable(reason: str, code: str, action: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "error_code": code, "action_needed": action}


def read_costs(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        with open(_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return _unavailable("AWS cost monitoring is not enabled on this deployment", "not_installed",
                            "Install the kopyya-aws-costs collector on the QA host (never on production).")
    except (OSError, ValueError) as exc:
        return _unavailable(f"collector file unreadable ({type(exc).__name__})", "unreadable",
                            "Check `journalctl -u kopyya-aws-costs` on the QA host.")
    if not isinstance(data, dict) or "status" not in data or "fetched_at" not in data:
        return _unavailable("collector file is malformed", "unreadable",
                            "Check `journalctl -u kopyya-aws-costs` on the QA host.")
    try:
        age = (now - datetime.fromisoformat(data["fetched_at"])).total_seconds()
    except (TypeError, ValueError):
        age = None
    data["age_seconds"] = None if age is None else max(0, int(age))
    data["stale"] = age is None or age > STALE_AFTER_S
    return data
