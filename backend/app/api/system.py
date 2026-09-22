"""Admin System → Usage: live host telemetry for the Lightsail box.

Reads CPU / memory / disk / swap / load / network of the instance this backend
runs on. In-container psutil reports HOST figures — CPU and memory aren't
namespaced, and the overlay root reflects the host partition — so no docker.sock
mount and no AWS credentials are needed. The endpoint reports the LOCAL box, so
prod's admin shows prod and QA's admin shows QA.

Admin-only (require_admin).
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.models.user import User

router = APIRouter(prefix="/api/admin/system", tags=["admin", "system"])

# CPU% is a rate — psutil needs a sampling window. Keep it short so the request
# stays snappy while still giving a real reading (a single call with no window
# returns 0.0 on the first hit).
_CPU_SAMPLE_SECONDS = 0.4


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

    return {
        "hostname": socket.gethostname(),
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - boot),
        "process_count": len(psutil.pids()),
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
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
        },
    }
