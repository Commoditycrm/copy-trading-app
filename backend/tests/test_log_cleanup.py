"""Clear Logs: the host agent only ever touches the approved operational logs
(container logs emptied in place, rotated system logs deleted), never security
logs, the journal or live system logs; it acts only on a confirmed, fresh
request matching the list the admin saw; dry-run until enabled; retention;
and the backend's request/audit side.

Runs on disposable logs in a temp directory — no real log is touched.
"""
import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

_AGENT = Path(__file__).resolve().parents[2] / "scripts" / "host_log_cleanup.py"


@pytest.fixture
def agent(monkeypatch):
    spec = importlib.util.spec_from_file_location(f"host_log_cleanup_{uuid.uuid4().hex}", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def host(agent, tmp_path, monkeypatch):
    c = tmp_path / "containers"
    v = tmp_path / "varlog"
    ops = tmp_path / "ops"
    out = tmp_path / "metrics"
    for d in (c / "aaa", c / "bbb", v / "journal", v / "apt", v / "private", ops / "requests", out):
        d.mkdir(parents=True)
    files = {
        c / "aaa" / "aaa-json.log": 3000, c / "aaa" / "aaa-json.log.1": 5000,      # worker
        c / "bbb" / "bbb-json.log": 7000,                                          # db
        v / "syslog": 100, v / "syslog.1": 400, v / "dmesg.1.gz": 50, v / "kern.log.1": 60,
        v / "apt" / "history.log.1.gz": 70,
        v / "auth.log.1": 900, v / "btmp.1": 10, v / "wtmp": 10, v / "lastlog": 10,
        v / "journal" / "system@1.journal": 800, v / "private" / "x.1": 5,
        v / "kopyya-ops-audit.log.1": 5,
    }
    for p, n in files.items():
        p.write_bytes(b"x" * n)
    monkeypatch.setattr(agent, "CONTAINERS_DIR", str(c))
    monkeypatch.setattr(agent, "VARLOG", str(v))
    monkeypatch.setattr(agent, "OPS_DIR", str(ops))
    monkeypatch.setattr(agent, "OUT_DIR", str(out))
    monkeypatch.setattr(agent, "AUDIT_LOG", str(tmp_path / "ops-audit.log"))
    monkeypatch.setattr(agent, "ENABLED", False)
    monkeypatch.setattr(agent, "_container_names", lambda: {"aaa": "copy-trading-app-worker-1", "bbb": "copy-trading-app-db-1"})
    return SimpleNamespace(c=c, v=v, ops=ops, out=out, files=files, audit=tmp_path / "ops-audit.log")


def request(host, inv, **kw):
    body = {"request_id": str(uuid.uuid4()), "inventory_id": inv["inventory_id"], "categories": ["app_logs"],
            "confirm": True, "requested_by": "admin@test.local",
            "requested_at": datetime.now(timezone.utc).isoformat()}
    body.update(kw)
    (host.ops / "requests" / "cleanup.json").write_text(json.dumps(body))
    return body


# ── what's eligible ───────────────────────────────────────────────────────


def test_only_approved_operational_logs_are_listed(agent, host):
    inv = agent.inventory()
    got = {(Path(i["path"]).name, i["category"], i["action"]) for i in inv["items"]}
    assert got == {
        ("aaa-json.log", "app_logs", "truncate"), ("aaa-json.log.1", "app_logs", "delete"),
        ("bbb-json.log", "docker_logs", "truncate"),
        ("syslog.1", "rotated_system_logs", "delete"), ("dmesg.1.gz", "rotated_system_logs", "delete"),
        ("kern.log.1", "rotated_system_logs", "delete"), ("history.log.1.gz", "rotated_system_logs", "delete"),
    }
    names = {Path(i["path"]).name for i in inv["items"]}
    for never in ("syslog", "auth.log.1", "btmp.1", "wtmp", "lastlog", "system@1.journal", "x.1", "kopyya-ops-audit.log.1"):
        assert never not in names
    assert inv["categories"]["app_logs"] == {"label": agent.CATEGORIES["app_logs"], "files": 2, "bytes": 8000}


def test_the_inventory_id_changes_when_the_files_change(agent, host):
    a = agent.inventory()["inventory_id"]
    (host.v / "syslog.2.gz").write_bytes(b"x")
    assert agent.inventory()["inventory_id"] != a


# ── requests ──────────────────────────────────────────────────────────────


def test_a_request_is_a_dry_run_until_enabled(agent, host):
    inv = agent.inventory()
    request(host, inv, categories=["app_logs", "rotated_system_logs"])
    res = agent.handle_request(inv)
    assert res["status"] == "dry_run" and res["dry_run"] is True
    assert res["freed_bytes"] == 8000 + 400 + 50 + 60 + 70
    for p, n in host.files.items():
        assert p.stat().st_size == n                                    # nothing touched
    assert json.loads(host.audit.read_text().splitlines()[-1])["status"] == "dry_run"
    assert not (host.ops / "requests" / "cleanup.json").exists()     # one request, one run


def test_enabled_clears_only_the_chosen_category_and_the_container_keeps_logging(agent, host, monkeypatch):
    monkeypatch.setattr(agent, "ENABLED", True)
    live = host.c / "aaa" / "aaa-json.log"
    writer = open(live, "ab")                                         # Docker holds it open for append
    inv = agent.inventory()
    request(host, inv, categories=["app_logs"])
    res = agent.handle_request(inv)
    assert res["status"] == "done" and res["freed_bytes"] == 8000
    assert live.stat().st_size == 0 and not (host.c / "aaa" / "aaa-json.log.1").exists()
    writer.write(b'{"log":"still logging"}\n')
    writer.close()
    assert live.read_bytes() == b'{"log":"still logging"}\n'         # appends land at the start, no hole
    assert (host.c / "bbb" / "bbb-json.log").stat().st_size == 7000  # another category, untouched
    assert (host.v / "syslog.1").exists() and (host.v / "auth.log.1").stat().st_size == 900


def test_a_changed_list_means_nothing_is_cleared(agent, host, monkeypatch):
    monkeypatch.setattr(agent, "ENABLED", True)
    shown = agent.inventory()
    request(host, shown)
    (host.c / "aaa" / "aaa-json.log.2").write_bytes(b"x" * 10)        # rotated since the admin looked
    res = agent.handle_request(agent.inventory())
    assert res["status"] == "refused" and "changed" in res["reason"]
    assert (host.c / "aaa" / "aaa-json.log").stat().st_size == 3000


@pytest.mark.parametrize("kw,reason", [
    ({"confirm": False}, "not confirmed"),
    ({"categories": ["database", "journal"]}, "no eligible categories"),
    ({"requested_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}, "expired"),
])
def test_bad_requests_are_refused(agent, host, monkeypatch, kw, reason):
    monkeypatch.setattr(agent, "ENABLED", True)
    inv = agent.inventory()
    request(host, inv, **kw)
    res = agent.handle_request(inv)
    assert res["status"] == "refused" and reason in res["reason"]
    for p, n in host.files.items():
        assert p.stat().st_size == n


def test_paths_in_a_request_are_ignored(agent, host, monkeypatch):
    monkeypatch.setattr(agent, "ENABLED", True)
    inv = agent.inventory()
    request(host, inv, categories=["rotated_system_logs"], paths=[str(host.v / "auth.log.1")],
            items=[{"path": str(host.v / "journal" / "system@1.journal"), "action": "delete"}])
    agent.handle_request(inv)
    assert (host.v / "auth.log.1").exists() and (host.v / "journal" / "system@1.journal").exists()
    assert not (host.v / "syslog.1").exists()


# ── retention ─────────────────────────────────────────────────────────────


def test_retention_deletes_only_old_rotated_system_logs(agent, host, monkeypatch):
    monkeypatch.setattr(agent, "ENABLED", True)
    old = time.time() - 30 * 86400
    for name in ("syslog.1", "dmesg.1.gz", "auth.log.1"):
        os.utime(host.v / name, (old, old))
    (host.ops / "retention.json").write_text(json.dumps({"enabled": True, "rotated_log_days": 14}))
    out = agent.apply_retention(agent.inventory())
    assert out["deleted"] == 2
    assert not (host.v / "syslog.1").exists() and not (host.v / "dmesg.1.gz").exists()
    assert (host.v / "kern.log.1").exists()                            # recent
    assert (host.v / "auth.log.1").exists()                            # protected, however old
    assert (host.c / "aaa" / "aaa-json.log.1").exists()                # Docker rotates its own


def test_retention_does_nothing_while_the_host_is_dry_run(agent, host):
    old = time.time() - 30 * 86400
    os.utime(host.v / "syslog.1", (old, old))
    (host.ops / "retention.json").write_text(json.dumps({"enabled": True, "rotated_log_days": 14}))
    assert agent.apply_retention(agent.inventory())["deleted"] == 0 and (host.v / "syslog.1").exists()


def test_main_writes_the_inventory_for_the_backend(agent, host):
    agent.main()
    inv = json.loads((host.out / "logs-inventory.json").read_text())
    assert inv["enabled"] is False and inv["retention"]["enabled"] is False
    assert all("mtime" not in i for i in inv["items"])


# ── backend ───────────────────────────────────────────────────────────────


@pytest.fixture
def backend(tmp_path, monkeypatch, agent, host):
    from app.database import SessionLocal
    from app.models.user import User, UserRole
    from app.services import log_cleanup as svc

    monkeypatch.setenv("HOST_METRICS_DIR", str(host.out))
    monkeypatch.setenv("HOST_OPS_DIR", str(host.ops))
    agent.main()
    db = SessionLocal()
    admin = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:10]}@test.local", password_hash="x", role=UserRole.ADMIN)
    db.add(admin)
    db.commit()
    yield SimpleNamespace(db=db, svc=svc, admin=admin,
                          inv=json.loads((host.out / "logs-inventory.json").read_text()))
    db.rollback()
    db.delete(db.get(User, admin.id))
    db.commit()
    db.close()


def test_a_confirmed_request_is_written_for_the_host_and_audited(backend, host):
    from app.models.audit_log import AuditLog
    b = backend
    out = b.svc.request_cleanup(b.db, b.admin.id, b.admin.email, ["app_logs"], b.inv["inventory_id"], True)
    assert out["files"] == 2 and out["expected_bytes"] == 8000 and out["dry_run"] is True
    req = json.loads((host.ops / "requests" / "cleanup.json").read_text())
    assert req["categories"] == ["app_logs"] and req["confirm"] is True and "paths" not in req
    row = b.db.execute(select(AuditLog).where(AuditLog.action == "system.logs_cleanup_requested",
                                              AuditLog.entity_id == out["request_id"])).scalar_one()
    assert row.actor_user_id == b.admin.id and row.metadata_json["expected_bytes"] == 8000


@pytest.mark.parametrize("cats,inv_ok,confirm,msg", [
    (["app_logs"], True, False, "Confirm"),
    (["journal"], True, True, "Only these"),
    (["app_logs", "database"], True, True, "Only these"),
    (["app_logs"], False, True, "changed"),
])
def test_requests_are_refused_unless_confirmed_eligible_and_current(backend, cats, inv_ok, confirm, msg):
    b = backend
    with pytest.raises(b.svc.CleanupRefused, match=msg):
        b.svc.request_cleanup(b.db, b.admin.id, b.admin.email, cats,
                              b.inv["inventory_id"] if inv_ok else "stale", confirm)


def test_one_request_at_a_time(backend):
    b = backend
    b.svc.request_cleanup(b.db, b.admin.id, b.admin.email, ["app_logs"], b.inv["inventory_id"], True)
    with pytest.raises(b.svc.CleanupRefused, match="already waiting"):
        b.svc.request_cleanup(b.db, b.admin.id, b.admin.email, ["app_logs"], b.inv["inventory_id"], True)


def test_the_hosts_result_is_audited_once(backend, agent, host):
    from app.models.audit_log import AuditLog
    b = backend
    out = b.svc.request_cleanup(b.db, b.admin.id, b.admin.email, ["app_logs"], b.inv["inventory_id"], True)
    agent.main()                                                      # the host picks it up (dry run)
    for _ in range(2):
        st = b.svc.status(b.db)
    assert st["last_result"]["status"] == "dry_run"
    rows = b.db.execute(select(AuditLog).where(AuditLog.action == "system.logs_cleanup_finished",
                                               AuditLog.entity_id == out["request_id"])).scalars().all()
    assert len(rows) == 1 and rows[0].metadata_json["freed_bytes"] == 8000


def test_retention_is_validated_saved_for_the_host_and_audited(backend, host):
    b = backend
    with pytest.raises(b.svc.CleanupRefused):
        b.svc.set_retention(b.db, b.admin.id, True, 0)
    assert b.svc.set_retention(b.db, b.admin.id, True, 30) == {"enabled": True, "rotated_log_days": 30}
    assert json.loads((host.ops / "retention.json").read_text())["rotated_log_days"] == 30


def test_no_agent_means_unavailable(tmp_path, monkeypatch):
    from app.services import log_cleanup as svc
    monkeypatch.setenv("HOST_METRICS_DIR", str(tmp_path))
    monkeypatch.setenv("HOST_OPS_DIR", str(tmp_path))
    assert svc.status(db=None)["status"] == "unavailable"


def test_the_endpoints_are_admin_only():
    from app.api import system
    from app.api.deps import require_admin
    for path in ("/logs", "/logs/cleanup", "/logs/retention"):
        route = next(r for r in system.router.routes if r.path.endswith(path))
        assert any(d.call is require_admin for d in route.dependant.dependencies), path
