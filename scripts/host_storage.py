#!/usr/bin/env python3
"""How the QA host's disk is used, for Admin → System → Storage.

Runs ON THE HOST as root from a systemd timer (scripts/systemd/
kopyya-host-storage.*), like host_metrics.py: the containers can't see the
host filesystem, so the backend only reads the JSON this writes through the
existing read-only /host-metrics mount.

Every category is measured on disk (du / stat), never estimated, and the
categories don't overlap: Docker's directories are counted once with the
Postgres/Redis volumes and the container logs taken out of them, /var/log is
counted without the journal, and "OS and other system files" is what's left
of the filesystem's used bytes — so the categories always add up to "used".
AWS snapshot storage is not on this disk; it's reported apart, from the AWS
cost collector's file.

Stdlib only. Writes atomically.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

OUT_DIR = os.environ.get("KOPYYA_METRICS_DIR", "/var/lib/kopyya-metrics")
APP_DIRS = [p for p in os.environ.get("KOPYYA_APP_DIRS", "/home/ubuntu/copy-trading-app:/usr/local/lib/kopyya").split(":") if p]
BACKUP_DIRS = [p for p in os.environ.get("KOPYYA_BACKUP_DIRS", "/home/ubuntu/backups:/var/backups").split(":") if p]
HOME = os.environ.get("KOPYYA_HOME", "/home/ubuntu")
ROOT = os.environ.get("KOPYYA_FS_ROOT", "/")
DOCKER_DIRS = ["/var/lib/docker", "/var/lib/containerd"]
SWAP = "/swapfile"
HISTORY_DAYS = 120
# containers whose stdout is the application's own log
APP_CONTAINERS = ("backend", "worker", "frontend", "discord-listener")


def du(paths: list[str]) -> int:
    """Bytes used on disk by these paths, staying on one filesystem; missing
    paths count 0. One du call, so hard links shared between them count once."""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return 0
    out = subprocess.run(["du", "-x", "-s", "-c", "-B1", *paths], capture_output=True, text=True, timeout=600)
    for line in reversed(out.stdout.splitlines()):
        if line.endswith("\ttotal") or line.endswith(" total"):
            return int(line.split()[0])
    return 0


def file_bytes(paths: list[str]) -> int:
    total = 0
    for p in paths:
        try:
            st = os.stat(p)
            blocks = getattr(st, "st_blocks", None)          # allocated, not apparent (POSIX)
            total += blocks * 512 if blocks is not None else st.st_size
        except OSError:
            pass
    return total


def fs_usage(root: str = ROOT) -> dict:
    st = os.statvfs(root)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    return {"total": total, "used": used, "available": free}


def container_names() -> dict[str, str]:
    try:
        out = subprocess.run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def container_logs(base: str = "/var/lib/docker/containers") -> list[dict]:
    names = container_names()
    out = []
    for path in glob.glob(f"{base}/*/*-json.log*"):
        cid = os.path.basename(os.path.dirname(path))
        name = names.get(cid, cid[:12])
        out.append({"path": path, "container": name, "bytes": file_bytes([path]),
                    "app": any(k in name for k in APP_CONTAINERS)})
    return out


def docker_df() -> dict:
    """Docker's own accounting, for detail only (its image sizes share layers)."""
    try:
        out = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    rows = {}
    for line in out.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        rows[r.get("Type", "?")] = {"size": r.get("Size"), "reclaimable": r.get("Reclaimable"),
                                    "count": r.get("TotalCount")}
    return rows


def largest(root: str = ROOT, depth: int = 3, n: int = 12) -> list[dict]:
    """The biggest directories at a fixed depth (so a parent and its child
    never both appear)."""
    try:
        out = subprocess.run(["du", "-x", "-B1", "-d", str(depth), root],
                             capture_output=True, text=True, timeout=900).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    base = len([p for p in root.split("/") if p])
    rows = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if len([p for p in path.split("/") if p]) - base == depth:
            rows.append({"path": path, "bytes": int(size)})
    return sorted(rows, key=lambda r: -r["bytes"])[:n]


def aws_snapshots(metrics_dir: str = OUT_DIR) -> dict | None:
    """EBS snapshot storage as billed this month, from the AWS cost collector."""
    try:
        with open(os.path.join(metrics_dir, "aws-costs.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    period = (data.get("periods") or [{}])[0]
    for c in period.get("categories") or []:
        if c.get("category") == "EBS Snapshots":
            return {"cost_usd": c.get("cost_usd"), "quantity": c.get("quantity"), "unit": c.get("unit"),
                    "month": period.get("month"), "attributed": True}
    excluded = sum(float(w.get("excluded_usd") or 0) for w in (data.get("attribution") or {}).get("warnings") or []
                   if "EBS Snapshots" in (w.get("categories") or []))
    return {"cost_usd": "0.00", "quantity": None, "unit": None, "month": period.get("month"),
            "attributed": False, "excluded_usd": f"{excluded:.2f}" if excluded else None}


def collect() -> dict:
    fs = fs_usage()
    pg_vols = glob.glob("/var/lib/docker/volumes/*pgdata*")
    redis_vols = glob.glob("/var/lib/docker/volumes/*redis*")
    logs = container_logs()
    app_log = sum(x["bytes"] for x in logs if x["app"])
    other_log = sum(x["bytes"] for x in logs if not x["app"])
    docker_total = du(DOCKER_DIRS)
    pg = du(pg_vols)
    redis = du(redis_vols)
    journal = du(["/var/log/journal"])
    varlog = du(["/var/log"])
    backups = du(BACKUP_DIRS) + file_bytes(
        [p for pat in ("*.dump", "*.sql", "*.sql.gz", "*.tar.gz") for p in glob.glob(os.path.join(HOME, pat))])
    swap = file_bytes([SWAP])
    cats = [
        ("app_code", "Application code & deployment files", du(APP_DIRS)),
        ("postgres", "PostgreSQL data", pg),
        ("redis", "Redis data", redis),
        # Docker's directories hold the volumes and the container logs too
        ("docker", "Docker images, layers & build cache", max(0, docker_total - pg - redis - app_log - other_log)),
        ("app_logs", "Application logs (backend, worker, frontend, Discord)", app_log),
        ("docker_logs", "Docker logs (database, Redis, Caddy, IBKR gateways)", other_log),
        ("system_logs", "System logs (/var/log and the journal)", varlog),
        ("backups", "Backups on this disk", backups),
        ("swap", "Swap file", swap),
    ]
    measured = sum(b for _, _, b in cats)
    cats.append(("os_other", "OS, packages & other system files", max(0, fs["used"] - measured)))
    return {
        "schema": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "filesystem": fs,
        "categories": [{"key": k, "label": label, "bytes": b,
                        "pct_of_used": round(100 * b / fs["used"], 2) if fs["used"] else None,
                        "pct_of_disk": round(100 * b / fs["total"], 2) if fs["total"] else None}
                       for k, label, b in cats],
        "detail": {
            "journal_bytes": journal,
            "redis_persistence": "off (no volume)" if not redis_vols else "volume present",
            "docker": docker_df(),
            "container_logs": sorted(({"container": x["container"], "bytes": x["bytes"]} for x in logs),
                                     key=lambda x: -x["bytes"])[:15],
        },
        "largest": largest(),
        "aws_snapshots": aws_snapshots(),
    }


def update_history(payload: dict, path: str) -> list[dict]:
    try:
        with open(path) as f:
            hist = json.load(f)
        if not isinstance(hist, list):
            hist = []
    except (OSError, ValueError):
        hist = []
    today = payload["collected_at"][:10]
    entry = {"date": today, "used": payload["filesystem"]["used"], "total": payload["filesystem"]["total"],
             "categories": {c["key"]: c["bytes"] for c in payload["categories"]}}
    hist = [h for h in hist if h.get("date") != today] + [entry]
    return sorted(hist, key=lambda h: h["date"])[-HISTORY_DAYS:]


def write_json(obj, path: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".storage.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main() -> int:
    t0 = time.monotonic()
    payload = collect()
    payload["took_s"] = round(time.monotonic() - t0, 1)
    write_json(payload, os.path.join(OUT_DIR, "storage.json"))
    hist_path = os.path.join(OUT_DIR, "storage-history.json")
    write_json(update_history(payload, hist_path), hist_path)
    print(f"storage: used={payload['filesystem']['used']} took={payload['took_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
