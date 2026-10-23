"""Admin → System → SMS costs: Twilio's billing and message log read through a
fake client (no Twilio calls), the server's own tracked texts, the labelled
estimate, caching, failures, and no credentials in the response."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.sms_notification import SmsNotification
from app.models.user import User, UserRole
from app.services import sms_costs as svc

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def fake_twilio(pages=None, fail=False):
    calls = []
    pages = pages or [{"messages": [
        {"direction": "outbound-api", "status": "delivered", "num_segments": "2", "price": "-0.0158"},
        {"direction": "outbound-api", "status": "undelivered", "error_code": 30034, "num_segments": "2", "price": "-0.0158"},
        {"direction": "outbound-api", "status": "undelivered", "error_code": 30034, "num_segments": "1", "price": "-0.0079"},
        {"direction": "outbound-api", "status": "read", "num_segments": "1", "price": None},
        {"direction": "inbound", "status": "received", "num_segments": "1", "price": "-0.0079"},
    ], "next_page_uri": None}]

    def get(path, params=None):
        calls.append((path, params))
        if fail:
            raise RuntimeError("twilio down")
        if path.endswith("Daily.json"):
            return {"usage_records": [
                {"start_date": "2026-10-22", "count": "10", "usage": "20", "price": "0.20", "price_unit": "usd"},
                {"start_date": "2026-10-21", "count": "5", "usage": "9", "price": "0.10", "price_unit": "usd"},
                {"start_date": "2026-10-23", "count": "1", "usage": "2", "price": "0.02", "price_unit": "usd"}]}
        if path.endswith("ThisMonth.json"):
            return {"usage_records": [{"count": "336", "usage": "691", "price": "6.4843", "price_unit": "usd"}]}
        if path.endswith("LastMonth.json"):
            return {"usage_records": [{"count": "908", "usage": "1609", "price": "30.357", "start_date": "2026-09-01"}]}
        return pages.pop(0)
    get.calls = calls
    return get


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(s, "twilio_auth_token", "secret-token-xyz")
    svc._cache.clear()                                                   # noqa: SLF001


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_twilio_billing_and_delivery_are_read_and_labelled(db):
    out = svc.build(db, NOW, fake_twilio())
    t = out["twilio_account"]
    assert out["status"] == "ok" and "every environment" in t["scope"]
    assert (t["messages"], t["segments"], t["cost_usd"]) == (336, 691, "6.4843")
    assert t["last_month"] == {"messages": 908, "cost_usd": "30.3570", "month": "2026-09"}
    assert [d["date"] for d in t["daily"]] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    d = t["delivery"]
    assert (d["sent"], d["delivered"], d["undelivered"], d["failed"]) == (4, 2, 2, 0)   # inbound excluded; read = delivered
    assert d["segments"] == 6 and d["cost_usd"] == "0.0395"
    assert t["unregistered_blocked"] == 2 and d["top_errors"] == [{"code": 30034, "count": 2}]


def test_the_projection_is_an_estimate_from_recent_days():
    days = [{"date": "2026-10-21", "cost": Decimal("0.10")}, {"date": "2026-10-22", "cost": Decimal("0.20")},
            {"date": "2026-10-23", "cost": Decimal("0.02")}]
    p = svc.projection(days, date(2026, 10, 23))
    # 0.32 so far + 0.15/day average of the complete days × 9 days left (23rd–31st)
    assert p["estimate_usd"] == "1.6700" and p["daily_average_usd"] == "0.1500"
    assert "average of the last 2 billed days" in p["basis"]
    assert svc.projection([], date(2026, 10, 1))["estimate_usd"] is None


def test_message_log_pages_are_followed():
    pages = [{"messages": [{"direction": "outbound-api", "status": "delivered", "num_segments": "1", "price": "-0.01"}],
              "next_page_uri": "/2010-04-01/Accounts/ACtest/Messages.json?Page=1"},
             {"messages": [{"direction": "outbound-api", "status": "failed", "num_segments": "1", "price": "-0.01"}],
              "next_page_uri": None}]
    get = fake_twilio(pages)
    m = svc.twilio_messages(date(2026, 10, 23), get)
    assert m["sent"] == 2 and m["status"] == {"delivered": 1, "failed": 1} and m["truncated"] is False
    assert get.calls[0][1]["DateSent>"] == "2026-10-01"


def test_the_servers_own_rejected_trade_texts_are_counted(db):
    u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:10]}@test.local", password_hash="x", role=UserRole.TRADER)
    db.add(u)
    db.flush()
    for status, seg, price in (("delivered", 2, "0.0158"), ("undelivered", 2, "0.0158"), ("skipped", None, None)):
        db.add(SmsNotification(id=uuid.uuid4(), user_id=u.id, category="trade_rejected", outcome="broker_rejected",
                               dedup_key=f"t:{uuid.uuid4()}", body="x", status=status, num_segments=seg,
                               price=Decimal(price) if price else None, created_at=NOW, updated_at=NOW))
    db.flush()
    t = svc.tracked(db, NOW.date())
    assert t["rejected_trade_texts"] == 2 and t["by_status"]["skipped"] == 1
    assert t["segments"] == 4 and t["cost"] == Decimal("0.0316")


def test_no_twilio_config_is_unavailable_not_zero(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "")
    out = svc.build(db, NOW, fake_twilio())
    assert out["status"] == "unavailable" and "this_server" in out


def test_results_are_cached_and_refresh_is_rate_limited(db, monkeypatch):
    get = fake_twilio()
    first = svc.read(db, now=NOW, get=get)
    n = len(get.calls)
    assert svc.read(db, refresh=True, now=NOW, get=get)["cached_for_s"] >= 0 and len(get.calls) == n   # < 5 min
    svc._cache["at"] -= svc.MIN_REFRESH_S + 1                             # noqa: SLF001
    get2 = fake_twilio()
    svc.read(db, refresh=True, now=NOW, get=get2)
    assert len(get2.calls) > 0 and first["status"] == "ok"


def test_a_twilio_failure_serves_the_last_figures_or_says_unavailable(db):
    assert svc.read(db, now=NOW, get=fake_twilio(fail=True))["status"] == "unavailable"
    svc.read(db, now=NOW, get=fake_twilio())
    svc._cache["at"] -= svc.CACHE_TTL_S + 1                               # noqa: SLF001
    out = svc.read(db, now=NOW, get=fake_twilio(fail=True))
    assert out["status"] == "ok" and "failed" in out["last_error"]


def test_no_credentials_in_the_response(db):
    out = svc.read(db, now=NOW, get=fake_twilio())
    blob = json.dumps(out)
    assert "secret-token-xyz" not in blob and "ACtest" not in blob


def test_the_endpoint_is_admin_only():
    from app.api import sms_costs
    from app.api.deps import require_admin
    route = next(r for r in sms_costs.router.routes if r.path.endswith("/sms-costs"))
    assert any(d.call is require_admin for d in route.dependant.dependencies)
