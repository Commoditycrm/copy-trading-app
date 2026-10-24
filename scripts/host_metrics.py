#!/usr/bin/env python3
"""Host + per-container telemetry for the admin System page.

Runs ON THE HOST as root from a systemd timer (see scripts/systemd/). The app
containers are hardened (non-root, read-only, no caps), so they get neither the
docker socket nor the host /proc; the backend only reads the JSON this writes,
through a read-only bind mount. Psutil inside the container already sees host
CPU / memory / disk / swap / uptime — what it can't see is the host NIC, the
host process table, the host's name and the other containers. That's this.

Stdlib only. Writes atomically so the backend never reads half a file.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import time

OUT = os.environ.get("KOPYYA_METRICS_FILE", "/var/lib/kopyya-metrics/host.json")

_UNITS = {
    "B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
}
_SIZE = re.compile(r"^\s*([0-9.]+)\s*([A-Za-z]+)\s*$")


def parse_size(s: str) -> int | None:
    """'286.9MiB' / '17.1GB' / '0B' → bytes (docker stats mixes SI and IEC)."""
    m = _SIZE.match(s or "")
    if not m or m.group(2) not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def parse_pair(s: str) -> tuple[int | None, int | None]:
    a, _, b = (s or "").partition("/")
    return parse_size(a), parse_size(b)


def parse_pct(s: str) -> float | None:
    try:
        return round(float((s or "").strip().rstrip("%")), 2)
    except ValueError:
        return None


def default_iface(route_text: str) -> str | None:
    for line in route_text.splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            return f[0]
    return None


def nic_counters(dev_text: str, iface: str | None) -> dict | None:
    for line in dev_text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            f = rest.split()
            return {
                "interface": iface,
                "bytes_recv": int(f[0]), "packets_recv": int(f[1]),
                "bytes_sent": int(f[8]), "packets_sent": int(f[9]),
            }
    return None


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=20, check=True,
    ).stdout


def containers() -> list[dict]:
    ids = _docker("ps", "-aq", "--no-trunc").split()
    if not ids:
        return []
    info = {c["Id"]: c for c in json.loads(_docker("inspect", *ids))}
    stats = {}
    for line in _docker("stats", "--no-stream", "--no-trunc", "--format", "{{json .}}").splitlines():
        s = json.loads(line)
        stats[s["ID"]] = s
    out = []
    for cid, c in info.items():
        st = stats.get(cid, {})
        mem_used, mem_limit = parse_pair(st.get("MemUsage", ""))
        net_rx, net_tx = parse_pair(st.get("NetIO", ""))
        state = c.get("State") or {}
        labels = (c.get("Config") or {}).get("Labels") or {}
        out.append({
            "name": c.get("Name", "").lstrip("/"),
            "service": labels.get("com.docker.compose.service"),
            "state": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "restarts": c.get("RestartCount", 0),
            "started_at": state.get("StartedAt"),
            "cpu_percent": parse_pct(st.get("CPUPerc", "")),
            "mem_used": mem_used,
            "mem_limit": mem_limit,
            "mem_percent": parse_pct(st.get("MemPerc", "")),
            "pids": int(st["PIDs"]) if str(st.get("PIDs", "")).isdigit() else None,
            "net_recv": net_rx,
            "net_sent": net_tx,
        })
    return sorted(out, key=lambda x: x["name"])


def collect() -> dict:
    with open("/proc/net/route") as f:
        iface = default_iface(f.read())
    with open("/proc/net/dev") as f:
        nic = nic_counters(f.read(), iface)
    try:
        ctrs, err = containers(), None
    except Exception as exc:  # noqa: BLE001 — host metrics still useful without docker
        ctrs, err = [], f"{type(exc).__name__}: {exc}"[:200]
    return {
        "collected_at": time.time(),
        "hostname": socket.gethostname(),
        "process_count": sum(1 for p in os.listdir("/proc") if p.isdigit()),
        "network": nic,
        "containers": ctrs,
        "containers_error": err,
    }


def main() -> None:
    data = collect()
    d = os.path.dirname(OUT)
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".host.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
