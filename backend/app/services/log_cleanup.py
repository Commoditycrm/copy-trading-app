"""Admin → System → Logs: what can be cleared, and asking the host to clear it.

The deleting is done on the host by scripts/host_log_cleanup.py, which decides
which files are eligible; the backend never touches host files. It reads the
agent's inventory and results from the read-only /host-metrics mount and
writes requests into /host-ops, its one writable
host directory. Every request and its outcome go into the audit log.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.services import audit

CATEGORIES = ("app_logs", "docker_logs", "rotated_system_logs")
# the agent refreshes every 5 min; older than this and we won't act on it
INVENTORY_MAX_AGE_S = 15 * 60


class CleanupRefused(Exception):
    pass


def _metrics_dir() -> str:
    return os.environ.get("HOST_METRICS_DIR", "/host-metrics")


def _ops_dir() -> str:
    return os.environ.get("HOST_OPS_DIR", "/host-ops")


def _read(path: str) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(obj: Any, path: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".req.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _age(iso: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    inv = _read(os.path.join(_metrics_dir(), "logs-inventory.json"))
    result = _read(os.path.join(_metrics_dir(), "logs-cleanup-result.json"))
    if result and result.get("request_id"):
        _record_outcome(db, result)
    pending = _read(os.path.join(_ops_dir(), "requests", "cleanup.json"))
    if inv is None:
        return {"status": "unavailable", "reason": "The log-cleanup agent isn't installed on this server.",
                "last_result": result}
    age = _age(inv.get("generated_at"), now)
    return {
        "status": "ok",
        "inventory_id": inv.get("inventory_id"),
        "generated_at": inv.get("generated_at"),
        "stale": age is None or age > INVENTORY_MAX_AGE_S,
        "enabled": bool(inv.get("enabled")),            # off = every request is a dry run
        "categories": inv.get("categories") or {},
        "items": inv.get("items") or [],
        "never_cleared": inv.get("never_cleared") or [],
        "docker_rotation": inv.get("docker_rotation"),
        "pending_request": pending.get("request_id") if isinstance(pending, dict) else None,
        "last_result": result,
    }


def request_cleanup(db: Session, admin_id: uuid.UUID, admin_email: str, categories: list[str],
                    inventory_id: str, confirm: bool, now: datetime | None = None) -> dict[str, Any]:
    """Queue one cleanup for the host agent. Refused unless confirmed, the
    categories are eligible, and the list the admin was shown is still the
    current one."""
    now = now or datetime.now(timezone.utc)
    if not confirm:
        raise CleanupRefused("Confirm the cleanup first.")
    cats = [c for c in dict.fromkeys(categories) if c in CATEGORIES]
    if not cats or len(cats) != len(set(categories)):
        raise CleanupRefused("Only these can be cleared: " + ", ".join(CATEGORIES) + ".")
    inv = _read(os.path.join(_metrics_dir(), "logs-inventory.json"))
    if inv is None:
        raise CleanupRefused("The log-cleanup agent isn't installed on this server.")
    age = _age(inv.get("generated_at"), now)
    if age is None or age > INVENTORY_MAX_AGE_S:
        raise CleanupRefused("The log list is out of date; reload and review it again.")
    if inv.get("inventory_id") != inventory_id:
        raise CleanupRefused("The log files changed since you reviewed them; review the list again.")
    req_path = os.path.join(_ops_dir(), "requests", "cleanup.json")
    if os.path.exists(req_path):
        raise CleanupRefused("A cleanup is already waiting for the host.")
    chosen = [i for i in inv.get("items") or [] if i.get("category") in cats]
    expected = sum(int(i.get("bytes") or 0) for i in chosen)
    request_id = str(uuid.uuid4())
    try:
        _write({"request_id": request_id, "inventory_id": inventory_id, "categories": cats, "confirm": True,
                "requested_by": admin_email, "requested_at": now.isoformat()}, req_path)
    except OSError as exc:
        raise CleanupRefused(f"The host request folder isn't writable ({type(exc).__name__}).") from exc
    audit.record(db, actor_user_id=admin_id, action="system.logs_cleanup_requested",
                 entity_type="host_logs", entity_id=request_id,
                 metadata={"categories": cats, "inventory_id": inventory_id, "files": len(chosen),
                           "expected_bytes": expected, "dry_run": not inv.get("enabled"),
                           "paths": [i["path"] for i in chosen][:200]})
    db.commit()
    return {"request_id": request_id, "files": len(chosen), "expected_bytes": expected,
            "dry_run": not inv.get("enabled")}


def _record_outcome(db: Session, result: dict) -> None:
    """Write the host's result into the audit log once per request."""
    rid = str(result.get("request_id"))
    done = db.execute(select(AuditLog.id).where(AuditLog.action == "system.logs_cleanup_finished",
                                                AuditLog.entity_id == rid).limit(1)).first()
    if done:
        return
    audit.record(db, actor_user_id=None, action="system.logs_cleanup_finished", entity_type="host_logs",
                 entity_id=rid, metadata={k: result.get(k) for k in
                                          ("status", "dry_run", "freed_bytes", "categories", "reason", "finished_at")}
                 | {"files": len(result.get("items") or [])})
    db.commit()

