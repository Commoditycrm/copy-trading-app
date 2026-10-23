#!/usr/bin/env python3
"""Clears approved operational logs on the QA host, when an admin asks.

Runs ON THE HOST as root (systemd timer + path unit, scripts/systemd/
kopyya-log-cleanup.*). The backend can't touch host files: it writes a request
into /var/lib/kopyya-ops (its only writable host mount) and reads back the
inventory and results this writes into /var/lib/kopyya-metrics.

What can ever be cleared — decided here, never taken from a request:
  app_logs     the Docker json logs of backend, worker, frontend, Discord
  docker_logs  the Docker json logs of the other containers (db, redis, caddy, IBKR)
               → the live file is truncated in place (Docker appends to it, so the
                 container keeps logging); rotated .log.N files are deleted
  rotated_system_logs  already-rotated files in /var/log (syslog.1, kern.log.1,
               dmesg.N.gz …) — never auth/btmp/wtmp/lastlog/faillog/audit, never
               the journal, never a live log
Nothing else: no database files, no trade or audit records, no security logs.

A request names categories and the inventory it was shown; if the inventory
has changed since (different files), nothing is cleared. Until
KOPYYA_LOG_CLEANUP_ENABLED=true (/etc/kopyya/log-cleanup.env) every request is
a dry run: the result says what would have been freed, nothing is touched.
Every request — run, dry or refused — is appended to the host audit log.

Docker's own rotation (daemon.json max-size/max-file) already caps the
container logs.

Stdlib only. Outputs written atomically.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

OPS_DIR = os.environ.get("KOPYYA_OPS_DIR", "/var/lib/kopyya-ops")
OUT_DIR = os.environ.get("KOPYYA_METRICS_DIR", "/var/lib/kopyya-metrics")
CONTAINERS_DIR = os.environ.get("KOPYYA_CONTAINERS_DIR", "/var/lib/docker/containers")
VARLOG = os.environ.get("KOPYYA_VARLOG_DIR", "/var/log")
AUDIT_LOG = os.environ.get("KOPYYA_OPS_AUDIT", "/var/log/kopyya-ops-audit.log")
ENABLED = os.environ.get("KOPYYA_LOG_CLEANUP_ENABLED", "").strip().lower() in ("1", "true", "yes")

CATEGORIES = {
    "app_logs": "Application logs (backend, worker, frontend, Discord)",
    "docker_logs": "Docker logs of the other containers (database, Redis, Caddy, IBKR)",
    "rotated_system_logs": "Rotated system logs in /var/log",
}
APP_CONTAINERS = ("backend", "worker", "frontend", "discord-listener")
ROTATED = ("*.[0-9]", "*.[0-9].gz", "*.[0-9][0-9]", "*.[0-9][0-9].gz", "*.gz")
# never eligible, whatever their name looks like
PROTECTED = ("auth.log*", "btmp*", "wtmp*", "lastlog*", "faillog*", "secure*", "audit*", "sudo*",
             "kopyya-ops-audit*")
PROTECTED_DIRS = ("journal", "audit", "private")
REQUEST_MAX_AGE_S = 15 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _container_names() -> dict[str, str]:
    try:
        out = subprocess.run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def _protected(name: str, rel_dir: str) -> bool:
    if any(rel_dir == d or rel_dir.startswith(d + "/") for d in PROTECTED_DIRS):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in PROTECTED)


def inventory(names: dict[str, str] | None = None) -> dict:
    """Every file that could be cleared, by category, with the action."""
    names = names if names is not None else _container_names()
    items: list[dict] = []
    for path in sorted(glob.glob(os.path.join(CONTAINERS_DIR, "*", "*-json.log*"))):
        cid = os.path.basename(os.path.dirname(path))
        name = names.get(cid, cid[:12])
        cat = "app_logs" if any(k in name for k in APP_CONTAINERS) else "docker_logs"
        live = path.endswith("-json.log")
        items.append({"category": cat, "path": path, "container": name, "bytes": _size(path),
                      "action": "truncate" if live else "delete"})
    for dirpath, dirnames, filenames in os.walk(VARLOG):
        rel = os.path.relpath(dirpath, VARLOG).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        dirnames[:] = [d for d in dirnames if not _protected("", f"{rel}/{d}".lstrip("/"))]
        if rel.count("/") >= 1:                                     # /var/log and one level below
            dirnames[:] = []
        for fn in sorted(filenames):
            if _protected(fn, rel):
                continue
            if any(fnmatch.fnmatch(fn, pat) for pat in ROTATED):
                p = os.path.join(dirpath, fn)
                items.append({"category": "rotated_system_logs", "path": p, "container": None,
                              "bytes": _size(p), "action": "delete"})
    ident = hashlib.sha256("\n".join(f"{i['category']}|{i['path']}|{i['action']}" for i in items).encode()).hexdigest()[:16]
    totals = {c: {"label": label, "files": 0, "bytes": 0} for c, label in CATEGORIES.items()}
    for i in items:
        totals[i["category"]]["files"] += 1
        totals[i["category"]]["bytes"] += i["bytes"]
    return {"inventory_id": ident, "generated_at": _now().isoformat(), "enabled": ENABLED,
            "categories": totals, "items": items,
            "never_cleared": ["the systemd journal", "auth/security logs (auth.log, btmp, wtmp, lastlog, faillog, audit)",
                              "live system logs (syslog, kern.log …)", "database files and backups",
                              "orders, fills and audit records (in the database)"]}


def _write(obj, path: str, mode: int = 0o644) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".logs.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _audit(entry: dict) -> None:
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps({"at": _now().isoformat(), **entry}) + "\n")


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def handle_request(inv: dict) -> dict | None:
    """Run the pending request against the inventory just taken. Returns the
    result written, or None when there's no request."""
    req_path = os.path.join(OPS_DIR, "requests", "cleanup.json")
    req = _read_json(req_path)
    if req is None:
        return None
    try:
        os.replace(req_path, os.path.join(OPS_DIR, "requests", f".done-{int(time.time())}.json"))
    except OSError:
        pass
    result = {"request_id": str(req.get("request_id", ""))[:64], "requested_by": str(req.get("requested_by", ""))[:120],
              "categories": [], "dry_run": not ENABLED, "freed_bytes": 0, "items": [],
              "finished_at": None, "status": "refused", "reason": None}
    cats = req.get("categories") if isinstance(req.get("categories"), list) else []
    cats = [c for c in cats if c in CATEGORIES]
    result["categories"] = cats
    try:
        age = (_now() - datetime.fromisoformat(req.get("requested_at"))).total_seconds()
    except (TypeError, ValueError):
        age = None
    if not cats:
        result["reason"] = "no eligible categories requested"
    elif req.get("confirm") is not True:
        result["reason"] = "not confirmed"
    elif age is None or age > REQUEST_MAX_AGE_S or age < -60:
        result["reason"] = "request expired"
    elif req.get("inventory_id") != inv["inventory_id"]:
        result["reason"] = "the log files changed since the list was shown; review it again"
    else:
        result["status"] = "dry_run" if not ENABLED else "done"
        for item in inv["items"]:
            if item["category"] not in cats:
                continue
            row = {"path": item["path"], "action": item["action"], "bytes": item["bytes"], "ok": True, "error": None}
            if ENABLED:
                try:
                    if item["action"] == "truncate":
                        os.truncate(item["path"], 0)               # Docker appends; the container keeps logging
                    else:
                        os.unlink(item["path"])
                except OSError as exc:
                    row["ok"], row["error"] = False, type(exc).__name__
            if row["ok"]:
                result["freed_bytes"] += item["bytes"]
            result["items"].append(row)
        if any(not r["ok"] for r in result["items"]):
            result["status"] = "partial"
    result["finished_at"] = _now().isoformat()
    _write(result, os.path.join(OUT_DIR, "logs-cleanup-result.json"))
    _audit({"event": "logs_cleanup", "request_id": result["request_id"], "by": result["requested_by"],
            "status": result["status"], "dry_run": result["dry_run"], "categories": cats,
            "freed_bytes": result["freed_bytes"], "files": len(result["items"]), "reason": result["reason"]})
    return result


def main() -> int:
    inv = inventory()
    handle_request(inv)
    inv = inventory()                                                 # what's there now
    inv["docker_rotation"] = (_read_json("/etc/docker/daemon.json") or {}).get("log-opts")
    _write(inv, os.path.join(OUT_DIR, "logs-inventory.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
