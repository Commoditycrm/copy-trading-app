"""Admin System → Usage: live telemetry for the host this backend runs on.

CPU / memory / swap / disk / uptime come from psutil in-container: those aren't
namespaced (and the overlay root reflects the host partition), so they are the
host's figures. Network counters, the process table and the hostname ARE
namespaced — read in-container they describe only the backend container. Those
come from the host collector (scripts/host_metrics.py, systemd timer) via a
read-only bind mount, together with per-container stats. No docker.sock, no
host /proc inside the hardened containers.

Without a fresh collector file the host network / processes are reported as
unavailable rather than passing the container's numbers off as the host's.

Admin-only (require_admin).
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.database import get_db
from app.models.user import User

router = APIRouter(prefix="/api/admin/system", tags=["admin", "system"])

# CPU% is a rate — psutil needs a sampling window. Keep it short so the request
# stays snappy while still giving a real reading (a single call with no window
# returns 0.0 on the first hit).
_CPU_SAMPLE_SECONDS = 0.4

# The collector runs every 15s; past this its numbers are no longer "live".
_HOST_METRICS_MAX_AGE_S = 60


def _host_metrics_path() -> str:
    return os.environ.get("HOST_METRICS_FILE", "/host-metrics/host.json")


def read_host_metrics(now: float | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(collector data or None, status). Data is None when missing, unreadable
    or stale — callers must not fall back to container-scoped values."""
    now = time.time() if now is None else now
    try:
        with open(_host_metrics_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, {"available": False, "reason": "collector not installed"}
    except (OSError, ValueError) as exc:
        return None, {"available": False, "reason": f"unreadable: {type(exc).__name__}"}
    collected = data.get("collected_at") if isinstance(data, dict) else None
    if not isinstance(collected, (int, float)):
        return None, {"available": False, "reason": "no collected_at"}
    age = max(0.0, now - collected)
    status: dict[str, Any] = {
        "available": age <= _HOST_METRICS_MAX_AGE_S,
        "collected_at": datetime.fromtimestamp(collected, timezone.utc).isoformat(),
        "age_seconds": round(age, 1),
    }
    if not status["available"]:
        status["reason"] = "collector stale"
        return None, status
    return data, status


@router.get("/usage")
def system_usage(_: User = Depends(require_admin)) -> dict[str, Any]:
    import psutil  # noqa: PLC0415 — heavy-ish import kept off the hot path

    # per-core over one window; overall is the mean so both cover the same span.
    per_core: list[float] = psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS, percpu=True)
    overall = round(sum(per_core) / len(per_core), 1) if per_core else 0.0

    try:
        load1, load5, load15 = psutil.getloadavg()  # Linux only
    except (OSError, AttributeError):
        load1 = load5 = load15 = None

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    boot = psutil.boot_time()
    host, host_status = read_host_metrics()
    host = host or {}
    host_net = host.get("network")

    return {
        "hostname": host.get("hostname") or socket.gethostname(),
        "hostname_scope": "host" if host.get("hostname") else "container",
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - boot),
        "process_count": host.get("process_count"),
        "host_metrics": host_status,
        "cpu": {
            "percent": overall,
            "cores": len(per_core),
            "per_core": [round(c, 1) for c in per_core],
            "load_avg": None if load1 is None else [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "available": vm.available,
            "percent": vm.percent,
        },
        "swap": {"total": sm.total, "used": sm.used, "percent": sm.percent},
        "disk": {
            "mount": "/",
            "total": du.total,
            "used": du.used,
            "free": du.free,
            "percent": du.percent,
        },
        # Host NIC since host boot; None without a fresh collector file.
        "network": None if not host_net else {
            "interface": host_net.get("interface"),
            "bytes_sent": host_net.get("bytes_sent"),
            "bytes_recv": host_net.get("bytes_recv"),
            "packets_sent": host_net.get("packets_sent"),
            "packets_recv": host_net.get("packets_recv"),
        },
        # This container's own eth0: includes backend↔db/redis traffic that
        # never leaves the host, and resets when the container restarts.
        "backend_container_network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
        },
        "containers": host.get("containers") or [],
        "containers_error": host.get("containers_error"),
    }


@router.get("/aws-costs")
def aws_costs(_: User = Depends(require_admin)) -> dict[str, Any]:
    """QA (EC2) AWS cost & usage, as last collected on the host from Cost
    Explorer. A file read — no AWS call — so reloading costs nothing. Returns
    status=unavailable (with the reason) rather than a number it can't back."""
    from app.services import aws_costs as svc  # noqa: PLC0415

    return svc.read_costs()


@router.get("/storage")
def storage(_: User = Depends(require_admin)) -> dict[str, Any]:
    """Host disk usage by category, measured on the host every 15 minutes. A
    file read; status=unavailable when the collector isn't installed."""
    from app.services import host_storage  # noqa: PLC0415

    return host_storage.read_storage()


# ── logs: clear (the host agent does the deleting) ─────────────


class CleanupIn(BaseModel):
    categories: list[str]
    inventory_id: str
    confirm: bool = False


@router.get("/logs")
def logs(db: Session = Depends(get_db), _: User = Depends(require_admin)) -> dict[str, Any]:
    """Which logs could be cleared (with every file and size), whether cleanup
    is live or dry-run on this host, the last result."""
    from app.services import log_cleanup  # noqa: PLC0415

    return log_cleanup.status(db)


@router.post("/logs/cleanup")
def logs_cleanup(body: CleanupIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> dict[str, Any]:
    from app.services import log_cleanup  # noqa: PLC0415

    try:
        return log_cleanup.request_cleanup(db, admin.id, admin.email, body.categories, body.inventory_id, body.confirm)
    except log_cleanup.CleanupRefused as exc:
        raise HTTPException(409, str(exc)) from exc

