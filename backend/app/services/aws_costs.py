"""AWS cost of the QA (EC2) environment, for Admin → System.

The backend never talks to AWS: scripts/aws_costs.py runs on the QA host with
the instance role and writes aws-costs.json into the read-only /host-metrics
mount. No collector file (production, or QA before it's installed) means
"unavailable", never $0.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any

SCHEMA = 2
# The collector runs every 8h; allow one missed run before calling it stale.
STALE_AFTER_S = 18 * 3600
# Cost Explorer normally lags under a day; three days behind means billing data
# stopped arriving even though the collector itself is running.
BILLING_STALE_AFTER_DAYS = 3


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
    if data.get("schema") != SCHEMA:
        # the old format mixed in account-wide and Lightsail figures; don't show it
        return _unavailable("the cost collector on the QA host is an older version", "collector_outdated",
                            "Install the current scripts/aws_costs.py on the QA host.")
    try:
        age = (now - datetime.fromisoformat(data["fetched_at"])).total_seconds()
    except (TypeError, ValueError):
        age = None
    data["age_seconds"] = None if age is None else max(0, int(age))
    data["stale"] = age is None or age > STALE_AFTER_S
    through = ((data.get("periods") or [{}])[0] or {}).get("data_through")
    try:
        lag = (now.date() - date.fromisoformat(through)).days if through else None
    except ValueError:
        lag = None
    data["billing_lag_days"] = lag
    data["billing_stale"] = data.get("status") == "ok" and (lag is None or lag > BILLING_STALE_AFTER_DAYS)
    return data
