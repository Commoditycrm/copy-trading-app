"""Admin → System AWS cost section: attribution, no double counting, prod
(Lightsail) exclusion, credits kept apart, unavailable states, and the backend
only ever reading the host collector's file. A fake Cost Explorer stands in for
AWS — no AWS calls, no Cost Explorer charges."""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import aws_costs as reader

_COLLECTOR = Path(__file__).resolve().parents[2] / "scripts" / "aws_costs.py"

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SEP, OCT = "2026-09-01", "2026-10-01"


@pytest.fixture(scope="module")
def svc():
    if not _COLLECTOR.exists():
        pytest.skip("scripts/aws_costs.py not in this checkout")
    spec = importlib.util.spec_from_file_location("aws_costs_collector", _COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def _g(keys, cost, qty=None, unit=""):
    m = {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}}
    if qty is not None:
        m["UsageQuantity"] = {"Amount": str(qty), "Unit": unit}
    return {"Keys": keys, "Metrics": m}


def _months(prev, cur):
    return [{"TimePeriod": {"Start": SEP, "End": OCT}, "Groups": prev, "Estimated": False},
            {"TimePeriod": {"Start": OCT, "End": "2026-10-23"}, "Groups": cur, "Estimated": True}]


ACCOUNT = _months(
    [_g(["Amazon Elastic Compute Cloud - Compute", "Usage"], 60), _g(["EC2 - Other", "Usage"], 10),
     _g(["Amazon Virtual Private Cloud", "Usage"], "3.60"), _g(["Amazon Lightsail", "Usage"], 40),
     _g(["Tax", "Tax"], 5)],
    [_g(["Amazon Elastic Compute Cloud - Compute", "Usage"], 45), _g(["EC2 - Other", "Usage"], 7),
     _g(["Amazon Virtual Private Cloud", "Usage"], "2.52"), _g(["Amazon Lightsail", "Usage"], 28),
     _g(["AmazonCloudWatch", "Usage"], "0.50"),
     _g(["Amazon Elastic Compute Cloud - Compute", "Credit"], "-30")],
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


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "x"}}


def cfg(svc, **kw):
    base = dict(region="us-east-2", instance_id="i-085045eefdda2e747", tag_key=None, tag_value=None,
                allocate_regional_ec2=False)
    base.update(kw)
    return svc.Config(**base)


def money(x):
    return Decimal(x) if x is not None else None


# ── attribution ───────────────────────────────────────────────────────────


def test_regional_allocation_counts_ec2_family_only_and_excludes_lightsail(svc):
    out = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True), TODAY, ec2=FakeEC2(["i-085045eefdda2e747"]))
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


def test_credits_are_reported_apart_and_never_reduce_the_qa_figures(svc):
    out = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True), TODAY)
    m = out["month_to_date"]
    assert out["basis"] == "before credits and refunds"
    assert money(m["account_credits_usd"]) == Decimal("-30.00")
    assert money(m["account_net_usd"]) == Decimal("53.02")
    assert money(m["account_total_usd"]) == Decimal("83.02")     # gross, credit row not mixed in
    assert money(m["qa_total_usd"]) == Decimal("49.52")
    assert "Credit" not in {r["service"] for r in out["unattributed_by_service"]}


def test_qa_scoped_queries_exclude_credits_and_refunds(svc):
    ce = FakeCE()
    svc.collect(ce, cfg(svc, allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    scoped = [kw for _, kw in ce.calls if kw.get("Filter")]
    assert len(scoped) == 4                                      # regional, tagged, daily, forecast
    for kw in scoped:
        assert json.dumps(svc.NOT_CREDITS) in json.dumps(kw["Filter"])


def test_tag_plus_allocation_never_double_counts(svc):
    out = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    m = out["month_to_date"]
    assert money(m["direct_usd"]) == Decimal("46.00")            # tagged instance + volume
    assert money(m["allocated_usd"]) == Decimal("3.52")          # untagged IPv4 + data transfer
    assert money(m["qa_total_usd"]) == Decimal("49.52")
    rows = {(s["category"]): s for s in out["services"]}
    assert money(rows["EC2 compute"]["direct_usd"]) == 40 and money(rows["EC2 compute"]["allocated_usd"]) == 0
    assert money(rows["Public IPv4"]["direct_usd"]) == 0 and money(rows["Public IPv4"]["allocated_usd"]) == Decimal("2.52")
    for s in out["services"]:
        assert money(s["cost_usd"]) == money(s["direct_usd"]) + money(s["allocated_usd"])


def test_tag_only_reports_direct_and_leaves_the_rest_unattributed(svc):
    out = svc.collect(FakeCE(), cfg(svc, tag_key="kopyya:env", tag_value="qa"), TODAY)
    m = out["month_to_date"]
    assert money(m["qa_total_usd"]) == 46 and m["allocated_usd"] is None
    assert money(m["unattributed_usd"]) == Decimal("37.02")


def test_no_attribution_source_never_shows_zero_as_qa_cost(svc):
    out = svc.collect(FakeCE(), cfg(svc), TODAY)
    assert out["status"] == "no_attribution"
    assert out["month_to_date"]["qa_total_usd"] is None
    assert out["previous_month"]["qa_total_usd"] is None
    assert out["projected_month_usd"] is None


def test_allocation_flagged_unverified_when_other_instances_exist(svc):
    out = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True), TODAY,
                      ec2=FakeEC2(["i-085045eefdda2e747", "i-other"]))
    v = out["attribution"]["allocation_verified"]
    assert v["verified"] is False and v["instances_in_region"] == 2


def test_previous_month_is_split_the_same_way(svc):
    out = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True), TODAY)
    p = out["previous_month"]
    assert p["month"] == "2026-09" and money(p["qa_total_usd"]) == Decimal("73.60")
    assert money(p["account_total_usd"]) == Decimal("118.60")
    assert money(p["account_credits_usd"]) == 0


# ── trend, projection ─────────────────────────────────────────────────────


def test_daily_trend_average_and_aws_forecast(svc):
    ce = FakeCE(forecast="20.00")
    out = svc.collect(ce, cfg(svc, allocate_regional_ec2=True), TODAY)
    assert len(out["daily"]) == 22 and out["daily"][-1]["estimated"] is True
    assert money(out["average_daily_usd"]) == Decimal("2.00")    # 21 complete days, today excluded
    assert out["projection_method"] == "aws_forecast"
    assert money(out["projected_month_usd"]) == Decimal("69.52")
    assert out["data_through"] == "2026-10-22"
    fc = next(kw for kind, kw in ce.calls if kind == "forecast")
    assert fc["TimePeriod"] == {"Start": "2026-10-23", "End": "2026-11-01"}


def test_forecast_failure_falls_back_to_a_labelled_linear_estimate(svc):
    out = svc.collect(FakeCE(forecast=AwsError("DataUnavailableException")), cfg(svc, allocate_regional_ec2=True), TODAY)
    assert out["projection_method"] == "linear_from_daily_average"
    assert money(out["projected_month_usd"]) == Decimal("67.52")  # 49.52 + 2.00 × 9 remaining days


def test_qa_scope_filters_never_include_lightsail(svc):
    ce = FakeCE()
    svc.collect(ce, cfg(svc, allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    scoped = [kw for kind, kw in ce.calls if kw.get("Filter")]
    assert scoped
    for kw in scoped:
        f = json.dumps(kw["Filter"])
        assert "Lightsail" not in f
        assert "us-east-2" in f or '"Tags"' in f


def test_requests_per_run_are_bounded(svc):
    """Cost Explorer bills $0.01/request; a full run stays at 5."""
    ce = FakeCE()
    svc.collect(ce, cfg(svc, allocate_regional_ec2=True, tag_key="kopyya:env", tag_value="qa"), TODAY)
    assert len(ce.calls) == 5


# ── collector run / output ────────────────────────────────────────────────


def test_access_denied_is_written_as_unavailable_with_the_fix(svc):
    out = svc.run(cfg(svc, allocate_regional_ec2=True), NOW,
                  clients=lambda c: (FakeCE(error=AwsError("AccessDeniedException")), None))
    assert out["status"] == "unavailable" and out["error_code"] == "AccessDeniedException"
    assert "ce:GetCostAndUsage" in out["action_needed"]
    assert "month_to_date" not in out and out["fetched_at"] == NOW.isoformat()


def test_missing_boto3_is_unavailable(svc):
    def no_boto(_):
        raise ImportError("boto3")
    out = svc.run(cfg(svc, allocate_regional_ec2=True), NOW, clients=no_boto)
    assert out["error_code"] == "boto3_missing"


def test_write_is_atomic_and_world_readable(svc, tmp_path):
    out = tmp_path / "m" / "aws-costs.json"
    svc.write({"status": "ok", "fetched_at": NOW.isoformat()}, str(out))
    assert json.loads(out.read_text())["status"] == "ok"
    assert [p.name for p in out.parent.iterdir()] == ["aws-costs.json"]   # no temp file left


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
def test_usage_types_map_to_dashboard_categories(svc, service, usage, category):
    assert svc.categorize(service, usage)[0] == category


# ── backend: reads the file, never AWS ────────────────────────────────────


@pytest.fixture
def cost_file(tmp_path, monkeypatch):
    p = tmp_path / "aws-costs.json"
    monkeypatch.setenv("AWS_COSTS_FILE", str(p))
    return p


def test_no_collector_file_means_not_enabled_never_zero(cost_file):
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "not_installed"
    assert "month_to_date" not in out


def test_fresh_file_is_served_with_its_age(svc, cost_file):
    payload = svc.collect(FakeCE(), cfg(svc, allocate_regional_ec2=True), TODAY)
    payload["fetched_at"] = (NOW - timedelta(hours=2)).isoformat()
    cost_file.write_text(json.dumps(payload))
    out = reader.read_costs(NOW)
    assert out["month_to_date"]["qa_total_usd"] == "49.52"
    assert out["age_seconds"] == 7200 and out["stale"] is False


def test_old_file_is_flagged_stale(cost_file):
    cost_file.write_text(json.dumps({"status": "ok", "fetched_at": (NOW - timedelta(hours=30)).isoformat()}))
    assert reader.read_costs(NOW)["stale"] is True


def test_collector_unavailable_payload_passes_through(cost_file):
    cost_file.write_text(json.dumps({"status": "unavailable", "error_code": "AccessDeniedException",
                                     "fetched_at": NOW.isoformat()}))
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "AccessDeniedException"


@pytest.mark.parametrize("body", ["{not json", "[]", '{"status": "ok"}'])
def test_garbage_file_is_unavailable(cost_file, body):
    cost_file.write_text(body)
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "unreadable"


def test_file_the_backend_cannot_read_is_unavailable(cost_file, monkeypatch):
    cost_file.write_text(json.dumps({"status": "ok", "fetched_at": NOW.isoformat()}))
    real_open = open

    def denied(path, *a, **kw):
        if str(path) == str(cost_file):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", denied)
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "unreadable"
    assert "PermissionError" in out["reason"]


def test_backend_has_no_aws_sdk_or_credentials_path():
    src = Path(reader.__file__).read_text(encoding="utf-8")
    assert "boto3" not in src and "169.254" not in src
    reqs = (Path(reader.__file__).resolve().parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert "boto3" not in reqs and "botocore" not in reqs


def test_endpoint_is_admin_only_and_takes_no_refresh():
    from app.api import system
    from app.api.deps import require_admin
    route = next(r for r in system.router.routes if r.path.endswith("/aws-costs"))
    assert any(d.call is require_admin for d in route.dependant.dependencies)
    assert not route.dependant.query_params
