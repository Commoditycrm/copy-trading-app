"""Admin System → Usage: host vs container scope.

Psutil in the backend container sees host CPU / memory / disk / uptime, but its
network counters, process table and hostname are the container's. Those must
come from the host collector, and be reported as unavailable — never replaced
by the container's figures — when the collector file is missing or stale.
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest

from app.api import system

_COLLECTOR = Path(__file__).resolve().parents[2] / "scripts" / "host_metrics.py"


def _write(path: Path, **overrides):
    data = {
        "collected_at": time.time(),
        "hostname": "ip-172-31-8-105",
        "process_count": 252,
        "network": {"interface": "enp39s0", "bytes_recv": 9_650_000_000, "packets_recv": 1,
                    "bytes_sent": 1_470_000_000, "packets_sent": 2},
        "containers": [{"name": "copy-trading-app-worker-1", "service": "worker", "state": "running",
                        "health": None, "restarts": 0, "pids": 49}],
        "containers_error": None,
    }
    data.update(overrides)
    path.write_text(json.dumps(data))


@pytest.fixture
def host_file(tmp_path, monkeypatch):
    p = tmp_path / "host.json"
    monkeypatch.setenv("HOST_METRICS_FILE", str(p))
    return p


def test_fresh_collector_file_supplies_the_host_values(host_file):
    _write(host_file)
    u = system.system_usage(None)
    assert u["hostname"] == "ip-172-31-8-105" and u["hostname_scope"] == "host"
    assert u["process_count"] == 252
    assert u["network"]["interface"] == "enp39s0"
    assert u["network"]["bytes_sent"] == 1_470_000_000
    assert u["containers"][0]["service"] == "worker"
    assert u["host_metrics"]["available"] is True
    # the container's own counters are still there, but separately
    assert set(u["backend_container_network"]) == {"bytes_sent", "bytes_recv"}


def test_missing_collector_reports_unavailable_not_container_values(host_file):
    u = system.system_usage(None)
    assert u["host_metrics"] == {"available": False, "reason": "collector not installed"}
    assert u["process_count"] is None
    assert u["network"] is None
    assert u["containers"] == []
    assert u["hostname_scope"] == "container"
    # host-wide psutil figures are unaffected
    assert u["cpu"]["cores"] >= 1 and u["uptime_seconds"] > 0


def test_stale_collector_is_not_shown_as_live(host_file):
    _write(host_file, collected_at=time.time() - 600)
    u = system.system_usage(None)
    assert u["host_metrics"]["available"] is False
    assert u["host_metrics"]["reason"] == "collector stale"
    assert u["network"] is None and u["process_count"] is None


def test_garbage_file_is_unavailable(host_file):
    host_file.write_text("{not json")
    data, status = system.read_host_metrics()
    assert data is None and status["available"] is False
    assert status["reason"].startswith("unreadable")


@pytest.fixture(scope="module")
def collector():
    if not _COLLECTOR.exists():
        pytest.skip("scripts/host_metrics.py not in this checkout")
    spec = importlib.util.spec_from_file_location("host_metrics", _COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_collector_parses_docker_stats_units(collector):
    assert collector.parse_size("286.9MiB") == int(286.9 * 1024**2)
    assert collector.parse_size("17.1GB") == int(17.1 * 1000**3)
    assert collector.parse_size("0B") == 0
    assert collector.parse_size("n/a") is None
    assert collector.parse_pair("3.55GB / 5.59GB") == (int(3.55e9), int(5.59e9))
    assert collector.parse_pct("1.12%") == 1.12
    assert collector.parse_pct("--") is None


def test_collector_reads_the_default_route_nic(collector):
    route = (
        "Iface\tDestination\tGateway\tFlags\n"
        "docker0\t000011AC\t00000000\t0001\n"
        "enp39s0\t00000000\t0101FEA9\t0003\n"
    )
    assert collector.default_iface(route) == "enp39s0"
    dev = (
        "Inter-|   Receive\n"
        " face |bytes packets\n"
        "    lo: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0\n"
        "enp39s0: 9650000000 7 0 0 0 0 0 0 1470000000 8 0 0 0 0 0 0\n"
    )
    nic = collector.nic_counters(dev, "enp39s0")
    assert nic["bytes_recv"] == 9_650_000_000 and nic["bytes_sent"] == 1_470_000_000
    assert nic["packets_recv"] == 7 and nic["packets_sent"] == 8
