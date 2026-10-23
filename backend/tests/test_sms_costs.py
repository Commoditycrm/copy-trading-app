"""Admin → System → SMS costs: Twilio's billing read through a fake client (no
Twilio calls), caching, failures, and no credentials in the response."""
import json
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.services import sms_costs as svc

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def fake_twilio(fail=False):
    calls = []

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
        raise AssertionError(f"unexpected Twilio call {path}")
    get.calls = calls
    return get


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(s, "twilio_auth_token", "secret-token-xyz")
    svc._cache.clear()                                                   # noqa: SLF001


def test_twilio_billing_is_read_and_labelled():
    get = fake_twilio()
    out = svc.build(NOW, get)
    assert out["status"] == "ok" and "every environment" in out["scope"]
    assert (out["messages"], out["segments"], out["cost_usd"]) == (336, 691, "6.4843")
    assert out["last_month"] == {"messages": 908, "cost_usd": "30.3570", "month": "2026-09"}
    assert [d["date"] for d in out["daily"]] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert all(p["Category"] == "sms" for _, p in get.calls)              # SMS only, never WhatsApp
    assert get.calls[0][1]["StartDate"] == "2026-10-01"


def test_no_twilio_config_is_unavailable_not_zero(monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "")
    get = fake_twilio()
    assert svc.build(NOW, get)["status"] == "unavailable" and get.calls == []


def test_results_are_cached_and_refresh_is_rate_limited():
    get = fake_twilio()
    first = svc.read(now=NOW, get=get)
    n = len(get.calls)
    assert svc.read(refresh=True, now=NOW, get=get)["cached_for_s"] >= 0 and len(get.calls) == n   # < 5 min
    svc._cache["at"] -= svc.MIN_REFRESH_S + 1                             # noqa: SLF001
    get2 = fake_twilio()
    svc.read(refresh=True, now=NOW, get=get2)
    assert len(get2.calls) > 0 and first["status"] == "ok"


def test_a_twilio_failure_serves_the_last_figures_or_says_unavailable():
    assert svc.read(now=NOW, get=fake_twilio(fail=True))["status"] == "unavailable"
    svc.read(now=NOW, get=fake_twilio())
    svc._cache["at"] -= svc.CACHE_TTL_S + 1                               # noqa: SLF001
    out = svc.read(now=NOW, get=fake_twilio(fail=True))
    assert out["status"] == "ok" and "failed" in out["last_error"]


def test_no_credentials_in_the_response():
    blob = json.dumps(svc.read(now=NOW, get=fake_twilio()))
    assert "secret-token-xyz" not in blob and "ACtest" not in blob


def test_the_endpoint_is_admin_only():
    from app.api import sms_costs
    from app.api.deps import require_admin
    route = next(r for r in sms_costs.router.routes if r.path.endswith("/sms-costs"))
    assert any(d.call is require_admin for d in route.dependant.dependencies)
