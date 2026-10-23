"""Admin → System → Storage: the host collector's categories add up to the
disk's used bytes with nothing counted twice, and the backend only reads the
file."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import host_storage as reader

_COLLECTOR = Path(__file__).resolve().parents[2] / "scripts" / "host_storage.py"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
GB = 10**9


@pytest.fixture(scope="module")
def hs():
    spec = importlib.util.spec_from_file_location("host_storage_collector", _COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def disk(hs, monkeypatch):
    """A fake host: sizes by path, so du() is exercised without a real disk."""
    sizes = {
        "/home/ubuntu/copy-trading-app": 1 * GB // 50, "/usr/local/lib/kopyya": 0,
        "/var/lib/docker": int(0.47 * GB), "/var/lib/containerd": int(20.68 * GB),
        "/var/lib/docker/volumes/copy-trading-app_pgdata": int(0.44 * GB),
        "/var/log": int(0.10 * GB), "/var/log/journal": int(0.079 * GB),
        "/home/ubuntu/backups": int(0.062 * GB), "/var/backups": 0,
    }

    def du(paths):
        return sum(sizes.get(p, 0) for p in paths)
    monkeypatch.setattr(hs, "du", du)
    monkeypatch.setattr(hs.glob, "glob", lambda pat: ["/var/lib/docker/volumes/copy-trading-app_pgdata"]
                        if "pgdata" in pat else [])
    monkeypatch.setattr(hs, "file_bytes", lambda paths: int(4.29 * GB) if paths == [hs.SWAP] else 0)
    monkeypatch.setattr(hs, "fs_usage", lambda root="/": {"total": int(102.9 * GB), "used": int(29.46 * GB),
                                                          "available": int(73.4 * GB)})
    monkeypatch.setattr(hs, "container_logs", lambda: [
        {"container": "copy-trading-app-discord-listener-1", "bytes": 15 * 10**6, "app": True},
        {"container": "copy-trading-app-worker-1", "bytes": 1 * 10**6, "app": True},
        {"container": "copy-trading-app-db-1", "bytes": 5 * 10**6, "app": False},
    ])
    return sizes


def test_categories_add_up_to_used_with_nothing_counted_twice(hs, disk):
    out = hs.collect()
    cats = {c["key"]: c["bytes"] for c in out["categories"]}
    assert sum(cats.values()) == out["filesystem"]["used"]
    # Postgres and the container logs live inside Docker's directories: taken out once
    assert cats["docker"] == disk["/var/lib/docker"] + disk["/var/lib/containerd"] - disk[
        "/var/lib/docker/volumes/copy-trading-app_pgdata"] - 21 * 10**6
    assert cats["postgres"] == disk["/var/lib/docker/volumes/copy-trading-app_pgdata"]
    assert cats["app_logs"] == 16 * 10**6 and cats["docker_logs"] == 5 * 10**6
    assert cats["redis"] == 0
    assert cats["swap"] == int(4.29 * GB)
    assert cats["os_other"] > 0                                  # the remainder, never negative
    assert [c["key"] for c in out["categories"]] == [
        "app_code", "postgres", "redis", "docker", "app_logs", "docker_logs", "system_logs", "backups", "swap", "os_other"]


def test_percentages_are_of_used_and_of_disk(hs, disk):
    out = hs.collect()
    assert round(sum(c["pct_of_used"] for c in out["categories"])) == 100
    swap = next(c for c in out["categories"] if c["key"] == "swap")
    assert swap["pct_of_disk"] == round(100 * int(4.29 * GB) / int(102.9 * GB), 2)


def test_measured_more_than_used_never_goes_negative(hs, disk, monkeypatch):
    monkeypatch.setattr(hs, "fs_usage", lambda root="/": {"total": 100, "used": 1, "available": 99})
    cats = {c["key"]: c["bytes"] for c in hs.collect()["categories"]}
    assert cats["os_other"] == 0


def test_container_logs_are_split_into_application_and_other(hs, tmp_path, monkeypatch):
    for cid, size in (("aaa", 3000), ("bbb", 5000)):
        d = tmp_path / cid
        d.mkdir()
        (d / f"{cid}-json.log").write_bytes(b"x" * size)
    monkeypatch.setattr(hs, "container_names", lambda: {"aaa": "copy-trading-app-worker-1", "bbb": "copy-trading-app-db-1"})
    logs = {x["container"]: x for x in hs.container_logs(str(tmp_path))}
    assert logs["copy-trading-app-worker-1"]["app"] is True and logs["copy-trading-app-db-1"]["app"] is False
    assert logs["copy-trading-app-db-1"]["bytes"] >= 5000


# ── backend reader ────────────────────────────────────────────────────────


@pytest.fixture
def mdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_METRICS_DIR", str(tmp_path))
    return tmp_path


def test_no_collector_means_unavailable(mdir):
    out = reader.read_storage(NOW)
    assert out["status"] == "unavailable" and "isn't installed" in out["reason"]


def test_a_fresh_file_is_served(mdir):
    (mdir / "storage.json").write_text(json.dumps({"collected_at": (NOW - timedelta(minutes=10)).isoformat(),
                                                   "filesystem": {"total": 9, "used": 3, "available": 6},
                                                   "categories": []}))
    out = reader.read_storage(NOW)
    assert out["status"] == "ok" and out["stale"] is False and out["filesystem"]["used"] == 3


def test_an_old_file_is_stale(mdir):
    (mdir / "storage.json").write_text(json.dumps({"collected_at": (NOW - timedelta(hours=3)).isoformat(),
                                                   "filesystem": {}, "categories": []}))
    assert reader.read_storage(NOW)["stale"] is True


@pytest.mark.parametrize("body", ["{bad", "[]", '{"filesystem": {}}'])
def test_a_broken_file_is_unavailable(mdir, body):
    (mdir / "storage.json").write_text(body)
    assert reader.read_storage(NOW)["status"] == "unavailable"


def test_the_endpoint_is_admin_only():
    from app.api import system
    from app.api.deps import require_admin
    route = next(r for r in system.router.routes if r.path.endswith("/storage"))
    assert any(d.call is require_admin for d in route.dependant.dependencies)
