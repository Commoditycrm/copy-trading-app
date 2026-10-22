"""Admin → System AWS cost section: attribution, no double counting, prod
(Lightsail) exclusion, unavailable states, caching. A fake Cost Explorer stands
in for AWS — no AWS calls, no Cost Explorer charges."""
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import aws_costs as svc

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SEP, OCT = "2026-09-01", "2026-10-01"


def _g(keys, cost, qty=None, unit=""):
    m = {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}}
    if qty is not None:
        m["UsageQuantity"] = {"Amount": str(qty), "Unit": unit}
    return {"Keys": keys, "Metrics": m}


def _months(prev, cur):
    return [{"TimePeriod": {"Start": SEP, "End": OCT}, "Groups": prev, "Estimated": False},
            {"TimePeriod": {"Start": OCT, "End": "2026-10-23"}, "Groups": cur, "Estimated": True}]


ACCOUNT = _months(
    [_g(["Amazon Elastic Compute Cloud - Compute"], 60), _g(["EC2 - Other"], 10),
     _g(["Amazon Virtual Private Cloud"], "3.60"), _g(["Amazon Lightsail"], 40), _g(["Tax"], 5)],
    [_g(["Amazon Elastic Compute Cloud - Compute"], 45), _g(["EC2 - Other"], 7),
     _g(["Amazon Virtual Private Cloud"], "2.52"), _g(["Amazon Lightsail"], 28), _g(["AmazonCloudWatch"], "0.50")],
)
REGIONAL = _months(
    [_g(["Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:m7i-flex.xlarge"], 60, 720, "Hrs"),
     _g(["EC2 - Other", "USE2-EBS:VolumeUsage.gp3"], 9, 100, "GB-Mo"),
     _g(["EC2 - Other", "USE2-DataTransfer-Out-Bytes"], 1, 11, "GB"),
     _g(["Amazon Virtual Private Cloud", "USE2-PublicIPv4:InUseAddress"], "3.60", 720, "Hrs")],
    [_g(["Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:m7i-flex.xlarge"], 40, 504, "Hrs"),
     _g(["EC2 - Other", "USE2-EBS:VolumeUsage.gp3"], 6, 70, "GB-Mo"),
     _g(["EC2 - Other", "USE2-DataTransfer-Out-Bytes"], 1, 11, "GB"),
     _g(["Amazon Virtual Private Cloud", "USE2-PublicIPv4:InUseAddress"], "2.52", 504, "Hrs")],
)
# the instance and its volume are tagged; IPv4 and data transfer aren't
TAGGED = _months(
    [_g(["Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:m7i-flex.xlarge"], 60, 720, "Hrs"),
     _g(["EC2 - Other", "USE2-EBS:VolumeUsage.gp3"], 9, 100, "GB-Mo")],
    [_g(["Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:m7i-flex.xlarge"], 40, 504, "Hrs"),
     _g(["EC2 - Other", "USE2-EBS:VolumeUsage.gp3"], 6, 70, "GB-Mo")],
)
DAILY = [{"TimePeriod": {"Start": f"2026-10-{d:02d}", "End": f"2026-10-{d + 1:02d}"},
          "Total": {"UnblendedCost": {"Amount": "2.00" if d < 22 else "1.00", "Unit": "USD"}},
          "Estimated": d >= 21} for d in range(1, 23)]


class FakeCE:
    def __init__(self, forecast="20.00", error=None):
        self.calls, self.forecast, self.error = [], forecast, error

    def get_cost_and_usage(self, **kw):
        self.calls.append(("usage", kw))
        if self.error:
            raise self.error
        f = json.dumps(kw.get("Filter") or {})
        if kw["Granularity"] == "DAILY":
            return {"ResultsByTime": DAILY}
        if not kw.get("Filter"):
            return {"ResultsByTime": ACCOUNT}
        if '"Tags"' in f:
            return {"ResultsByTime": TAGGED}
        return {"ResultsByTime": REGIONAL}

    def get_cost_forecast(self, **kw):
        self.calls.append(("forecast", kw))
        if isinstance(self.forecast, Exception):
            raise self.forecast
        return {"Total": {"Amount": self.forecast, "Unit": "USD"}}


class FakeEC2:
    def __init__(self, ids):
        self.ids = ids

    def describe_instances(self, **kw):
        return {"Reservations": [{"Instances": [{"InstanceId": i} for i in self.ids]}]}


class FakeRedis:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return False
        self.d[k] = v
        return True

    def delete(self, k):
        self.d.pop(k, None)


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "x"}}


def cfg(**kw):
    base = dict(enabled=True, region="us-east-2", instance_id="i-085045eefdda2e747", tag_key=None, tag_value=None,
                allocate_regional_ec2=False, cache_ttl_s=43200, refresh_min_s=3600)
    base.update(kw)
    return svc.Config(**base)


def money(x):
    return Decimal(x) if x is not None else None


# ── attribution ───────────────────────────────────────────────────────────


def test_regional_allocation_counts_ec2_family_only_and_excludes_lightsail():
    out = svc.collect(FakeCE(), cfg(allocate_regional_ec2=True), TODAY, ec2=FakeEC2(["i-085045eefdda2e747"]))
    m = out["month_to_date"]
    assert out["status"] == "ok"
    assert money(m["qa_total_usd"]) == Decimal("49.52")          # 40 + 6 + 1 + 2.52
    assert money(m["allocated_usd"]) == Decimal("49.52") and m["direct_usd"] is None
    assert money(m["production_lightsail_excluded_usd"]) == Decimal("28.00")
    assert money(m["account_total_usd"]) == Decimal("83.02")
    # nothing counted twice: QA + unattributed == account
    assert money(m["qa_total_usd"]) + money(m["unattributed_usd"]) == money(m["account_total_usd"])
    assert sum(Decimal(s["cost_usd"]) for s in out["services"]) == money(m["qa_total_usd"])
    unat = {r["service"]: r for r in out["unattributed_by_service"]}
    assert unat["Amazon Lightsail"]["production"] is True and money(unat["Amazon Lightsail"]["cost_usd"]) == 28
    assert money(unat["Amazon Elastic Compute Cloud - Compute"]["cost_usd"]) == 5   # another region: not QA
    assert out["attribution"]["allocation_verified"]["verified"] is True


def test_tag_plus_allocation_never_double_counts():
    out = svc.collect(FakeCE(), cfg(allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    m = out["month_to_date"]
    assert money(m["direct_usd"]) == Decimal("46.00")            # tagged instance + volume
    assert money(m["allocated_usd"]) == Decimal("3.52")          # untagged IPv4 + data transfer
    assert money(m["qa_total_usd"]) == Decimal("49.52")
    rows = {(s["category"]): s for s in out["services"]}
    assert money(rows["EC2 compute"]["direct_usd"]) == 40 and money(rows["EC2 compute"]["allocated_usd"]) == 0
    assert money(rows["Public IPv4"]["direct_usd"]) == 0 and money(rows["Public IPv4"]["allocated_usd"]) == Decimal("2.52")
    for s in out["services"]:
        assert money(s["cost_usd"]) == money(s["direct_usd"]) + money(s["allocated_usd"])


def test_tag_only_reports_direct_and_leaves_the_rest_unattributed():
    out = svc.collect(FakeCE(), cfg(tag_key="kopyya:env", tag_value="qa"), TODAY)
    m = out["month_to_date"]
    assert money(m["qa_total_usd"]) == 46 and m["allocated_usd"] is None
    assert money(m["unattributed_usd"]) == Decimal("37.02")


def test_no_attribution_source_never_shows_zero_as_qa_cost():
    out = svc.collect(FakeCE(), cfg(), TODAY)
    assert out["status"] == "no_attribution"
    assert out["month_to_date"]["qa_total_usd"] is None
    assert out["previous_month"]["qa_total_usd"] is None
    assert out["projected_month_usd"] is None


def test_allocation_flagged_unverified_when_other_instances_exist():
    out = svc.collect(FakeCE(), cfg(allocate_regional_ec2=True), TODAY, ec2=FakeEC2(["i-085045eefdda2e747", "i-other"]))
    v = out["attribution"]["allocation_verified"]
    assert v["verified"] is False and v["instances_in_region"] == 2


def test_previous_month_is_split_the_same_way():
    out = svc.collect(FakeCE(), cfg(allocate_regional_ec2=True), TODAY)
    p = out["previous_month"]
    assert p["month"] == "2026-09" and money(p["qa_total_usd"]) == Decimal("73.60")
    assert money(p["account_total_usd"]) == Decimal("118.60")


# ── trend, projection ─────────────────────────────────────────────────────


def test_daily_trend_average_and_aws_forecast():
    ce = FakeCE(forecast="20.00")
    out = svc.collect(ce, cfg(allocate_regional_ec2=True), TODAY)
    assert len(out["daily"]) == 22 and out["daily"][-1]["estimated"] is True
    assert money(out["average_daily_usd"]) == Decimal("2.00")    # 21 complete days, today excluded
    assert out["projection_method"] == "aws_forecast"
    assert money(out["projected_month_usd"]) == Decimal("69.52")
    assert out["data_through"] == "2026-10-22"
    fc = next(kw for kind, kw in ce.calls if kind == "forecast")
    assert fc["TimePeriod"] == {"Start": "2026-10-23", "End": "2026-11-01"}


def test_forecast_failure_falls_back_to_a_labelled_linear_estimate():
    out = svc.collect(FakeCE(forecast=AwsError("DataUnavailableException")), cfg(allocate_regional_ec2=True), TODAY)
    assert out["projection_method"] == "linear_from_daily_average"
    assert money(out["projected_month_usd"]) == Decimal("67.52")  # 49.52 + 2.00 × 9 remaining days


def test_qa_scope_filters_never_include_lightsail():
    ce = FakeCE()
    svc.collect(ce, cfg(allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    scoped = [kw for kind, kw in ce.calls if kw.get("Filter")]
    assert scoped
    for kw in scoped:
        f = json.dumps(kw["Filter"])
        assert "Lightsail" not in f
        assert "us-east-2" in f or '"Tags"' in f


# ── unavailable / disabled / cache ────────────────────────────────────────


def test_access_denied_is_unavailable_with_the_fix_not_zero():
    r = FakeRedis()
    out = svc.get_costs(r, cfg=cfg(allocate_regional_ec2=True), now=NOW,
                        clients=lambda c: (FakeCE(error=AwsError("AccessDeniedException")), None))
    assert out["status"] == "unavailable" and out["error_code"] == "AccessDeniedException"
    assert "ce:GetCostAndUsage" in out["action_needed"]
    assert "month_to_date" not in out
    assert out["cache_ttl_seconds"] == svc.UNAVAILABLE_TTL_S


def test_no_credentials_is_unavailable():
    out = svc.get_costs(FakeRedis(), cfg=cfg(allocate_regional_ec2=True), now=NOW,
                        clients=lambda c: (FakeCE(error=AwsError("NoCredentialsError")), None))
    assert out["status"] == "unavailable" and "instance role" in out["action_needed"]


def test_disabled_deployment_never_calls_aws():
    called = []
    out = svc.get_costs(FakeRedis(), cfg=cfg(enabled=False), now=NOW, clients=lambda c: called.append(1))
    assert out["status"] == "unavailable" and out["error_code"] == "disabled" and called == []


def test_cached_result_is_reused_and_refresh_is_throttled():
    r, ce = FakeRedis(), FakeCE()
    c = cfg(allocate_regional_ec2=True)
    first = svc.get_costs(r, cfg=c, now=NOW, clients=lambda _: (ce, None))
    n = len(ce.calls)
    again = svc.get_costs(r, cfg=c, now=NOW.replace(hour=13), clients=lambda _: (ce, None))
    assert len(ce.calls) == n and again["month_to_date"] == first["month_to_date"]
    throttled = svc.get_costs(r, cfg=c, refresh=True, now=NOW.replace(minute=30), clients=lambda _: (ce, None))
    assert len(ce.calls) == n and throttled["refresh_throttled"] is True
    svc.get_costs(r, cfg=c, refresh=True, now=NOW.replace(hour=14), clients=lambda _: (ce, None))
    assert len(ce.calls) > n


def test_requests_per_refresh_are_bounded():
    """Cost Explorer bills $0.01/request; a full refresh stays at 5."""
    ce = FakeCE()
    svc.collect(ce, cfg(allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    assert len(ce.calls) == 5


# ── classification + endpoint ─────────────────────────────────────────────


@pytest.mark.parametrize("service,usage,category", [
    ("Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:m7i-flex.xlarge", "EC2 compute"),
    ("EC2 - Other", "USE2-EBS:VolumeUsage.gp3", "EBS volumes"),
    ("EC2 - Other", "EBS:VolumeUsage.gp3", "EBS volumes"),               # us-east-1 has no prefix
    ("EC2 - Other", "USE2-EBS:SnapshotUsage", "EBS snapshots"),
    ("Amazon Virtual Private Cloud", "USE2-PublicIPv4:InUseAddress", "Public IPv4"),
    ("EC2 - Other", "USE2-DataTransfer-Out-Bytes", "Data transfer"),
    ("EC2 - Other", "USE2-USW2-AWS-Out-Bytes", "Data transfer"),
    ("EC2 - Other", "USE2-NatGateway-Hours", "NAT gateway"),
    ("AmazonCloudWatch", "USE2-CW:MetricMonitorUsage", "CloudWatch"),
])
def test_usage_types_map_to_dashboard_categories(service, usage, category):
    assert svc.categorize(service, usage)[0] == category


def test_endpoint_is_admin_only():
    from app.api import system
    from app.api.deps import require_admin
    route = next(r for r in system.router.routes if r.path.endswith("/aws-costs"))
    assert any(d.call is require_admin for d in route.dependant.dependencies)
