"""Admin → System AWS cost section: QA-only attribution from discovered
resources, replaced/previous resources, exclusion of unrelated and unprovable
charges, data-transfer grouping, no double counting, the run-rate projection,
and the backend only ever reading the host collector's file.

A fake Cost Explorer that honours the collector's filters stands in for AWS,
with Lightsail, tax and another region in the same fake account — no AWS calls,
no Cost Explorer charges."""
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import aws_costs as reader

_COLLECTOR = Path(__file__).resolve().parents[2] / "scripts" / "aws_costs.py"

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ME = {"instance_id": "i-0aaaaaaaaaaaaaaa1", "region": "us-east-2"}
BOX, EBS, IP_USE, IP_IDLE = ("USE2-BoxUsage:m7i-flex.xlarge", "USE2-EBS:VolumeUsage.gp3",
                             "USE2-PublicIPv4:InUseAddress", "USE2-PublicIPv4:IdleAddress")
COMPUTE_SVC, OTHER_SVC, VPC_SVC = "Amazon Elastic Compute Cloud - Compute", "EC2 - Other", "Amazon Virtual Private Cloud"
HOURLY = Decimal("0.19152")
GB_MO = Decimal("0.08")
IP_H = Decimal("0.005")


@pytest.fixture(scope="module")
def svc():
    if not _COLLECTOR.exists():
        pytest.skip("scripts/aws_costs.py not in this checkout")
    spec = importlib.util.spec_from_file_location("aws_costs_collector", _COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


# ── a fake account ────────────────────────────────────────────────────────


def r(day, service, usage, qty, cost=None, unit="Hrs", region="us-east-2", tags=(), record="Usage"):
    rate = {BOX: HOURLY, EBS: GB_MO, IP_USE: IP_H, IP_IDLE: IP_H}.get(usage, Decimal("0.01"))
    qty = Decimal(str(qty))
    return {"day": day, "service": service, "usage": usage, "qty": qty,
            "cost": Decimal(str(cost)) if cost is not None else qty * rate,
            "unit": unit, "region": region, "tags": dict(tags), "record": record}


def compute(day, h, **kw):
    return r(day, COMPUTE_SVC, BOX, h, **kw)


def volume(day, gb_mo, **kw):
    return r(day, OTHER_SVC, EBS, gb_mo, unit="GB-Mo", **kw)


def ipv4(day, h, idle=False, **kw):
    return r(day, VPC_SVC, IP_IDLE if idle else IP_USE, h, **kw)


def unrelated(day):
    """Same account, never QA: Lightsail (production), tax, another region, a credit."""
    return [r(day, "Amazon Lightsail", "USE2-BundleUsage:16GB", 48, cost="5.42"),
            r(day, "Tax", "Tax", 0, cost="0.50", record="Tax"),
            r(day, COMPUTE_SVC, "BoxUsage:t3.micro", 24, cost="0.25", region="us-east-1"),
            r(day, COMPUTE_SVC, BOX, 0, cost="-1.00", record="Credit")]


def _match(row, f):
    if f is None:
        return True
    if "And" in f:
        return all(_match(row, x) for x in f["And"])
    if "Not" in f:
        return not _match(row, f["Not"])
    if "Dimensions" in f:
        key, vals = f["Dimensions"]["Key"], f["Dimensions"]["Values"]
        return {"REGION": row["region"], "SERVICE": row["service"], "RECORD_TYPE": row["record"]}[key] in vals
    if "Tags" in f:
        return row["tags"].get(f["Tags"]["Key"]) in f["Tags"]["Values"]
    raise AssertionError(f)


class FakeCE:
    def __init__(self, rows, error=None):
        self.rows, self.error, self.calls = rows, error, []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        start = date.fromisoformat(kw["TimePeriod"]["Start"])
        end = date.fromisoformat(kw["TimePeriod"]["End"])
        rows = [x for x in self.rows if start <= x["day"] < end and _match(x, kw.get("Filter"))]
        if kw["Granularity"] == "MONTHLY":
            total = sum((x["cost"] for x in rows), Decimal(0))
            return {"ResultsByTime": [{"TimePeriod": kw["TimePeriod"],
                                       "Total": {"UnblendedCost": {"Amount": str(total), "Unit": "USD"}}}]}
        out, d = [], start
        while d < end:
            groups: dict = {}
            for x in rows:
                if x["day"] == d:
                    g = groups.setdefault((x["service"], x["usage"]), [Decimal(0), Decimal(0), x["unit"]])
                    g[0] += x["cost"]
                    g[1] += x["qty"]
            out.append({"TimePeriod": {"Start": d.isoformat(), "End": (d + timedelta(days=1)).isoformat()},
                        "Estimated": True,
                        "Groups": [{"Keys": list(k), "Metrics": {
                            "UnblendedCost": {"Amount": str(v[0]), "Unit": "USD"},
                            "UsageQuantity": {"Amount": str(v[1]), "Unit": v[2]}}} for k, v in groups.items()]})
            d += timedelta(days=1)
        return {"ResultsByTime": out}


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "x"}}


def inst(iid, itype="m7i-flex.xlarge", state="running", attached="2026-10-14T07:00:00+00:00", ip=True, tags=None):
    return {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state},
            "LaunchTime": datetime(2026, 10, 21, 5, tzinfo=timezone.utc),       # resets on start
            "BlockDeviceMappings": [{"Ebs": {"VolumeId": f"vol-{iid}", "AttachTime": attached}}],
            "PublicIpAddress": "203.0.113.10" if ip else None, "Tags": tags or []}


def vol(iid, size=100, vtype="gp3", created="2026-10-14T07:00:00+00:00", iops=3000, tput=125):
    return {"VolumeId": f"vol-{iid}", "VolumeType": vtype, "Size": size, "Iops": iops, "Throughput": tput,
            "CreateTime": created, "Attachments": [{"InstanceId": iid}] if iid else []}


class FakeEC2:
    def __init__(self, instances, volumes, snapshots=None, addresses=None):
        self.instances, self.volumes, self.snapshots, self.addresses = instances, volumes, snapshots, addresses

    def describe_instances(self, **kw):
        return {"Reservations": [{"Instances": self.instances}]}

    def describe_volumes(self, **kw):
        return {"Volumes": self.volumes}

    def describe_addresses(self, **kw):
        if self.addresses is None:
            raise AwsError("UnauthorizedOperation")
        return {"Addresses": self.addresses}

    def describe_snapshots(self, **kw):
        if self.snapshots is None:
            raise AwsError("UnauthorizedOperation")
        return {"Snapshots": self.snapshots}


def current_qa():
    return FakeEC2([inst(ME["instance_id"])], [vol(ME["instance_id"])])


def cfg(svc, **kw):
    base = dict(region=None, tag_key=None, tag_value=None)
    base.update(kw)
    return svc.Config(**base)


def collect(svc, rows, ec2=None, ledger=None, now=NOW, **cfg_kw):
    ce = FakeCE(rows)
    out = svc.collect(ce, ec2 or current_qa(), cfg(svc, **cfg_kw), ME, ledger if ledger is not None else {},
                      now)
    return out, ce


def D(x):
    return Decimal(x) if x is not None else None


def cat(period, name):
    return next((c for c in period["categories"] if c["category"] == name), None)


# The real QA October: migration resources 12–13 Oct that no longer exist, the
# current instance from 14 Oct 07:00, stopped 17 Oct, running again 21 Oct.
OCT = date(2026, 10, 1)


def oct_rows():
    rows = []
    for d in range(1, 22):
        rows += unrelated(date(2026, 10, d))
    rows += [compute(date(2026, 10, 12), 8), volume(date(2026, 10, 12), "3.87"),
             r(date(2026, 10, 12), OTHER_SVC, "USE2-EBS:SnapshotUsage", "1.0", unit="GB-Mo", cost="0.05"),
             compute(date(2026, 10, 13), 12), volume(date(2026, 10, 13), "11.889"),
             compute(date(2026, 10, 14), 17), volume(date(2026, 10, 14), "13.549"), ipv4(date(2026, 10, 14), 17)]
    for d in (15, 16):
        rows += [compute(date(2026, 10, d), 24), volume(date(2026, 10, d), "3.2258"), ipv4(date(2026, 10, d), 24)]
    for d in (17, 18, 19, 20):
        rows += [volume(date(2026, 10, d), "3.2258"), ipv4(date(2026, 10, d), 24, idle=True)]
    rows += [compute(date(2026, 10, 21), 18), volume(date(2026, 10, 21), "3.2258"),
             ipv4(date(2026, 10, 21), 18), ipv4(date(2026, 10, 21), 6, idle=True),
             r(date(2026, 10, 21), OTHER_SVC, "USE2-DataTransfer-Regional-Bytes", "3.0", unit="GB", cost="0.03"),
             r(date(2026, 10, 21), OTHER_SVC, "USE2-USW2-AWS-Out-Bytes", "0.5", unit="GB", cost="0.01"),
             r(date(2026, 10, 21), OTHER_SVC, "USE2-DataTransfer-Out-Bytes", "1.0", unit="GB", cost="0.09")]
    return rows


# ── QA-only attribution ───────────────────────────────────────────────────


def test_payload_carries_no_production_account_or_unrelated_figures(svc):
    out, _ = collect(svc, oct_rows())
    blob = json.dumps(out)
    for word in ("Lightsail", "Tax", "account", "BundleUsage", "t3.micro", "us-east-1", "Credit"):
        assert word not in blob, word
    assert {c["category"] for p in out["periods"] for c in p["categories"]} <= set(svc.CATEGORIES)


def test_qa_total_is_the_sum_of_the_rows_shown(svc):
    out, _ = collect(svc, oct_rows())
    cur = out["periods"][0]
    assert D(cur["qa_total_usd"]) == sum(D(c["cost_usd"]) for c in cur["categories"])
    assert D(cur["qa_total_usd"]) == sum(D(d["cost_usd"]) for d in cur["daily"]) or \
        abs(D(cur["qa_total_usd"]) - sum(D(d["cost_usd"]) for d in cur["daily"])) <= Decimal("0.05")


def test_nothing_is_dropped_or_counted_twice(svc):
    out, _ = collect(svc, oct_rows())
    rec = out["periods"][0]["reconciliation"]
    assert rec["balanced"] is True
    assert D(rec["attributed_usd"]) + D(rec["excluded_usd"]) == D(rec["billed_in_scope_usd"])
    assert rec["matches_cost_explorer"] is True
    assert D(rec["cost_explorer_monthly_usd"]) == D(rec["billed_in_scope_usd"])


def test_usage_within_the_qa_instance_lifetime_is_qa(svc):
    out, _ = collect(svc, oct_rows())
    days = {d["date"]: d for d in out["periods"][0]["daily"]}
    # 14 Oct: instance from 07:00 → 17 h of capacity, 17 h billed
    assert D(days["2026-10-14"]["cost_usd"]) >= (17 * HOURLY).quantize(Decimal("0.01"))
    # stopped days: volume + idle address only, all QA
    assert D(days["2026-10-18"]["excluded_usd"]) == 0
    assert D(days["2026-10-18"]["cost_usd"]) == (Decimal("3.2258") * GB_MO + 24 * IP_H).quantize(Decimal("0.01"))


def test_charges_that_no_known_qa_resource_could_have_made_are_excluded_with_a_warning(svc):
    out, _ = collect(svc, oct_rows())
    days = {d["date"]: d for d in out["periods"][0]["daily"]}
    assert D(days["2026-10-12"]["cost_usd"]) == 0                # before any QA resource existed
    assert D(days["2026-10-12"]["excluded_usd"]) > 0
    assert D(days["2026-10-14"]["excluded_usd"]) > Decimal("0.8")  # 13.5 GB-Mo billed vs 100 GiB × 17 h
    w = {x["code"]: x for x in out["attribution"]["warnings"]}
    assert w["no_qa_resource"]["from"] == "2026-10-12" and D(w["no_qa_resource"]["excluded_usd"]) > 0
    assert out["attribution"]["complete"] is False
    cur = out["periods"][0]
    assert D(cur["excluded_usd"]) == sum(D(x["excluded_usd"]) for x in out["attribution"]["warnings"]
                                         if x["period"] == cur["month"])


def test_other_instances_in_the_region_are_not_qa_and_make_transfer_ambiguous(svc):
    rows = oct_rows() + [r(date(2026, 10, 21), COMPUTE_SVC, "USE2-BoxUsage:t3.large", 24, cost="2.00")]
    ec2 = FakeEC2([inst(ME["instance_id"]), inst("i-0bbbbbbbbbbbbbbb2", "t3.large", attached="2026-10-20T00:00:00+00:00")],
                  [vol(ME["instance_id"]), vol("i-0bbbbbbbbbbbbbbb2", size=8)])
    out, _ = collect(svc, rows, ec2=ec2)
    cur = out["periods"][0]
    assert all("t3.large" not in i["usage_type"] for c in cur["categories"] for i in c["items"])
    assert cat(cur, "Data Transfer") is None
    assert "transfer_ambiguous" in {w["code"] for w in out["attribution"]["warnings"]}
    assert [i["id"] for i in out["environment"]["instances"]] == [ME["instance_id"]]


# ── dynamic discovery, replacement, history ───────────────────────────────


def test_no_instance_specific_values_in_the_collector(svc):
    src = _COLLECTOR.read_text(encoding="utf-8")
    assert not re.search(r"\b(i|vol|eipalloc|snap)-[0-9a-f]{8,}\b", src)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", src.replace("169.254.169.254", ""))
    assert "instance_id" not in {f for f in svc.Config.__dataclass_fields__}


def test_environment_reflects_what_is_discovered_now(svc):
    ec2 = FakeEC2([inst(ME["instance_id"], "m7i.2xlarge")], [vol(ME["instance_id"], size=200)])
    out, _ = collect(svc, oct_rows(), ec2=ec2)
    env = out["environment"]
    assert env["instances"] == [{"id": ME["instance_id"], "type": "m7i.2xlarge", "state": "running", "public_ipv4": True}]
    assert env["volumes"][0]["size_gib"] == 200 and env["public_ipv4"] == 1


def test_a_replaced_instance_keeps_its_charges_for_the_period(svc):
    """i-old ran 1–10 Oct as QA (recorded in the ledger by earlier runs), then was
    replaced by this host. Its hours stay QA though it's gone from AWS."""
    ledger: dict = {}
    old = FakeEC2([inst("i-0ccccccccccccccc3", "t3.large", attached="2026-10-01T00:00:00+00:00")],
                  [vol("i-0ccccccccccccccc3", size=50, created="2026-10-01T00:00:00+00:00")])
    svc.update_ledger(ledger, svc.discover(old, {"instance_id": "i-0ccccccccccccccc3", "region": "us-east-2"},
                                           cfg(svc), set()), datetime(2026, 10, 10, 23, tzinfo=timezone.utc))
    rows = [r(date(2026, 10, d), COMPUTE_SVC, "USE2-BoxUsage:t3.large", 24, cost="2.00") for d in range(1, 11)]
    rows += [volume(date(2026, 10, d), "1.6129") for d in range(1, 11)]
    rows += oct_rows()
    out, _ = collect(svc, rows, ledger=ledger)
    cur = out["periods"][0]
    items = {i["usage_type"]: i for c in cur["categories"] for i in c["items"]}
    assert D(items["BoxUsage:t3.large"]["cost_usd"]) == Decimal("20.0000")
    assert [i["id"] for i in out["environment"]["instances"]] == [ME["instance_id"]]   # only what exists now
    days = {d["date"]: d for d in cur["daily"]}
    assert D(days["2026-10-05"]["excluded_usd"]) == 0


def test_a_resize_keeps_both_instance_types(svc):
    ledger: dict = {}
    small = FakeEC2([inst(ME["instance_id"], "m7i-flex.large")], [vol(ME["instance_id"])])
    svc.update_ledger(ledger, svc.discover(small, ME, cfg(svc), set()), datetime(2026, 10, 18, tzinfo=timezone.utc))
    rows = [r(date(2026, 10, 15), COMPUTE_SVC, "USE2-BoxUsage:m7i-flex.large", 24, cost="2.30"),
            compute(date(2026, 10, 21), 18)]
    out, _ = collect(svc, rows, ledger=ledger)
    items = {i["usage_type"] for c in out["periods"][0]["categories"] for i in c["items"]}
    assert {"BoxUsage:m7i-flex.large", "BoxUsage:m7i-flex.xlarge"} <= items
    assert D(out["periods"][0]["excluded_usd"]) == 0


def test_previous_month_uses_the_same_rules(svc):
    ledger: dict = {}
    sep = FakeEC2([inst(ME["instance_id"], attached="2026-09-20T00:00:00+00:00")],
                  [vol(ME["instance_id"], created="2026-09-20T00:00:00+00:00")])
    svc.update_ledger(ledger, svc.discover(sep, ME, cfg(svc), set()), datetime(2026, 9, 30, tzinfo=timezone.utc))
    rows = [compute(date(2026, 9, d), 24) for d in range(15, 31)] + oct_rows()
    out, _ = collect(svc, rows, ledger=ledger)
    prev = out["periods"][1]
    assert prev["month"] == "2026-09"
    assert D(prev["qa_total_usd"]) == (11 * 24 * HOURLY).quantize(Decimal("0.01"))    # 20–30 Sep
    assert D(prev["excluded_usd"]) == (5 * 24 * HOURLY).quantize(Decimal("0.01"))     # 15–19 Sep
    assert prev["reconciliation"]["balanced"] is True


def test_ledger_round_trip_is_private(svc, tmp_path):
    p = tmp_path / "state" / "ledger.json"
    ledger = svc.update_ledger({}, svc.discover(current_qa(), ME, cfg(svc), set()), NOW)
    svc.save_ledger(ledger, str(p))
    assert svc.load_ledger(str(p)) == ledger
    if sys.platform != "win32":
        assert (p.stat().st_mode & 0o777) == 0o600
    assert svc.load_ledger(str(tmp_path / "missing.json")) == {"resources": {}}


# ── categories and data transfer ──────────────────────────────────────────


def test_data_transfer_is_one_row_with_a_breakdown_by_direction(svc):
    out, _ = collect(svc, oct_rows())
    xfer = cat(out["periods"][0], "Data Transfer")
    assert sum(1 for c in out["periods"][0]["categories"] if c["category"] == "Data Transfer") == 1
    assert D(xfer["cost_usd"]) == Decimal("0.13")
    assert xfer["unit"] == "GB" and D(xfer["quantity"]) == Decimal("4.5")
    desc = {i["description"]: i for i in xfer["items"]}
    assert set(desc) == {"Within region (between AZs)", "Out to other AWS regions", "Out to internet"}
    assert desc["Out to other AWS regions"]["regions"] == ["us-west-2"]
    assert desc["Out to internet"]["charge_type"] == "Per GB" and D(desc["Out to internet"]["quantity"]) == 1


def test_repeated_transfer_entries_collapse_by_direction(svc):
    day = date(2026, 10, 21)
    rows = oct_rows() + [
        r(day, OTHER_SVC, "USE2-EUC1-AWS-Out-Bytes", "0.2", unit="GB", cost="0.004"),
        r(day, OTHER_SVC, "USE2-APS1-AWS-Out-Bytes", "0.1", unit="GB", cost="0.002"),
        r(day, OTHER_SVC, "USE2-DataTransfer-In-Bytes", "5", unit="GB", cost="0"),
        r(day, "AWS Data Transfer", "USE2-DataTransfer-In-Bytes", "2", unit="GB", cost="0"),
    ]
    xfer = cat(collect(svc, rows)[0]["periods"][0], "Data Transfer")
    descs = [i["description"] for i in xfer["items"]]
    assert len(descs) == len(set(descs)) == 4
    out = next(i for i in xfer["items"] if i["description"] == "Out to other AWS regions")
    assert out["regions"] == ["ap-southeast-1", "eu-central-1", "us-west-2"] and D(out["quantity"]) == Decimal("0.8")
    assert D(next(i for i in xfer["items"] if i["description"] == "In from internet")["quantity"]) == 7


def test_incompatible_units_are_never_added_together(svc):
    rows = oct_rows() + [r(date(2026, 10, 21), OTHER_SVC, "USE2-EBS:VolumeP-IOPS.gp3", "10", unit="IOPS-Mo", cost="0.05")]
    ec2 = FakeEC2([inst(ME["instance_id"])], [vol(ME["instance_id"], iops=4000)])
    out, _ = collect(svc, rows, ec2=ec2)
    vols = cat(out["periods"][0], "EBS Volumes")
    assert vols["quantity"] is None and vols["unit"] is None
    assert {i["unit"] for i in vols["items"]} == {"GB-Mo", "IOPS-Mo"}
    ip = cat(out["periods"][0], "Public IPv4")
    assert ip["unit"] == "Hrs"              # in-use and idle are both address-hours


@pytest.mark.parametrize("service,usage,category", [
    (COMPUTE_SVC, "USE2-BoxUsage:m7i-flex.xlarge", "EC2 Compute"),
    (OTHER_SVC, "USE2-EBS:VolumeUsage.gp3", "EBS Volumes"),
    (OTHER_SVC, "EBS:VolumeUsage.gp3", "EBS Volumes"),                    # us-east-1 has no prefix
    (OTHER_SVC, "USE2-EBS:VolumeP-IOPS.gp3", "EBS Volumes"),
    (OTHER_SVC, "USE2-EBS:SnapshotUsage", "EBS Snapshots"),
    (VPC_SVC, "USE2-PublicIPv4:IdleAddress", "Public IPv4"),
    (OTHER_SVC, "USE2-DataTransfer-Out-Bytes", "Data Transfer"),
    (OTHER_SVC, "USE2-APS1-AWS-Out-Bytes", "Data Transfer"),
    (OTHER_SVC, "USE2-NatGateway-Hours", "Other verified QA services"),
])
def test_usage_types_map_to_dashboard_categories(svc, service, usage, category):
    assert svc.categorize(service, usage)[0] == category


@pytest.mark.parametrize("usage,direction", [
    ("USE2-DataTransfer-Regional-Bytes", "Within region (between AZs)"),
    ("USE2-DataTransfer-In-Bytes", "In from internet"),
    ("USE2-EUC1-AWS-Out-Bytes", "Out to AWS eu-central-1"),
    ("USE2-USE1-AWS-In-Bytes", "In from AWS us-east-1"),
])
def test_transfer_directions(svc, usage, direction):
    assert svc.transfer_direction(usage) == direction


# ── snapshots and missing permissions ─────────────────────────────────────


def test_snapshots_are_excluded_when_they_cant_be_tied_to_qa(svc):
    rows = oct_rows() + [r(date(2026, 10, 21), OTHER_SVC, "USE2-EBS:SnapshotUsage", "2", unit="GB-Mo", cost="0.10")]
    out, _ = collect(svc, rows)                                   # DescribeSnapshots denied
    assert cat(out["periods"][0], "EBS Snapshots") is None
    assert "snapshots_unverified" in {w["code"] for w in out["attribution"]["warnings"]}
    assert "ec2:DescribeSnapshots" in out["environment"]["unreadable"]
    assert out["environment"]["snapshots"] is None


def test_snapshots_of_qa_volumes_are_qa(svc):
    rows = oct_rows() + [r(date(2026, 10, 21), OTHER_SVC, "USE2-EBS:SnapshotUsage", "2", unit="GB-Mo", cost="0.10")]
    snaps = [{"SnapshotId": "snap-1", "VolumeId": f"vol-{ME['instance_id']}", "StartTime": "2026-10-20T00:00:00+00:00"}]
    ec2 = FakeEC2([inst(ME["instance_id"])], [vol(ME["instance_id"])], snapshots=snaps, addresses=[])
    out, _ = collect(svc, rows, ec2=ec2)
    assert D(cat(out["periods"][0], "EBS Snapshots")["cost_usd"]) == Decimal("0.10")
    assert out["environment"]["snapshots"] == 1 and out["environment"]["unreadable"] == []


def test_a_foreign_snapshot_makes_that_days_snapshot_cost_ambiguous(svc):
    rows = oct_rows() + [r(date(2026, 10, 21), OTHER_SVC, "USE2-EBS:SnapshotUsage", "2", unit="GB-Mo", cost="0.10")]
    snaps = [{"SnapshotId": "snap-1", "VolumeId": f"vol-{ME['instance_id']}", "StartTime": "2026-10-20T00:00:00+00:00"},
             {"SnapshotId": "snap-2", "VolumeId": "vol-elsewhere", "StartTime": "2026-10-01T00:00:00+00:00"}]
    ec2 = FakeEC2([inst(ME["instance_id"])], [vol(ME["instance_id"])], snapshots=snaps, addresses=[])
    out, _ = collect(svc, rows, ec2=ec2)
    assert cat(out["periods"][0], "EBS Snapshots") is None


# ── run rate and projection ───────────────────────────────────────────────


def test_run_rate_comes_from_current_resources_at_billed_prices(svc):
    out, _ = collect(svc, oct_rows())
    rr = out["run_rate"]
    assert rr["available"] is True
    comp = {c["category"]: D(c["daily_usd"]) for c in rr["components"]}
    assert comp["EC2 Compute"] == (HOURLY * 24).quantize(Decimal("0.0001"))
    assert comp["EBS Volumes"] == (100 * GB_MO / 31).quantize(Decimal("0.0001"))
    assert comp["Public IPv4"] == (IP_H * 24).quantize(Decimal("0.0001"))
    assert comp["Data Transfer"] > 0
    assert D(rr["daily_usd"]) == sum(comp.values()).quantize(Decimal("0.01"))


def test_projection_is_actual_plus_run_rate_for_the_rest_of_the_month(svc):
    out, _ = collect(svc, oct_rows())
    cur, pj = out["periods"][0], out["projection"]
    assert cur["data_through"] == "2026-10-21" and pj["remaining_days"] == 10
    assert pj["method"] == "actual_plus_run_rate"
    assert D(pj["month_usd"]) == (D(cur["qa_total_usd"]) + D(out["run_rate"]["daily_usd"]) * 10).quantize(Decimal("0.01"))
    # the old figure averaged over 1–11 Oct, when QA didn't exist yet
    naive = D(cur["qa_total_usd"]) / 21 * 31
    assert D(pj["month_usd"]) > naive + 10


def test_a_stopped_instance_adds_no_compute_to_the_run_rate(svc):
    ec2 = FakeEC2([inst(ME["instance_id"], state="stopped")], [vol(ME["instance_id"])])
    out, _ = collect(svc, oct_rows(), ec2=ec2)
    comp = [c for c in out["run_rate"]["components"] if c["category"] == "EC2 Compute"]
    assert len(comp) == 1 and D(comp[0]["daily_usd"]) == 0 and "stopped" in comp[0]["basis"]


def test_projection_is_unavailable_rather_than_guessed_without_a_billed_price(svc):
    ec2 = FakeEC2([inst(ME["instance_id"], "m7i.4xlarge")], [vol(ME["instance_id"])])
    out, _ = collect(svc, oct_rows(), ec2=ec2)
    assert out["run_rate"]["available"] is False and out["run_rate"]["daily_usd"] is None
    assert out["projection"]["month_usd"] is None and "m7i.4xlarge" in out["projection"]["reason"]


# ── tag mode ──────────────────────────────────────────────────────────────


def test_tagged_costs_include_resources_from_before_the_collector(svc):
    tag = {"kopyya:env": "qa"}
    rows = [x | {"tags": tag} for x in oct_rows() if x["service"] in (COMPUTE_SVC, OTHER_SVC, VPC_SVC)
            and x["region"] == "us-east-2" and x["record"] == "Usage"]
    rows += [r(date(2026, 10, 21), OTHER_SVC, "USE2-NatGateway-Hours", 24, cost="1.08")]       # untagged
    rows += [x for x in oct_rows() if x["service"] in ("Amazon Lightsail", "Tax") or x["region"] != "us-east-2"]
    out, ce = collect(svc, rows, tag_key="kopyya:env", tag_value="qa")
    cur = out["periods"][0]
    assert out["attribution"]["mode"] == "tag"
    days = {d["date"]: d for d in cur["daily"]}
    assert D(days["2026-10-12"]["cost_usd"]) > 0          # migration resources, tagged → QA
    assert {w["code"] for w in out["attribution"]["warnings"]} == {"untagged"}
    assert D(cur["excluded_usd"]) == Decimal("1.08")
    assert cur["reconciliation"]["balanced"] is True
    assert len(ce.calls) == 3


def test_a_tag_without_billing_data_falls_back_to_discovery(svc):
    out, _ = collect(svc, oct_rows(), tag_key="kopyya:env", tag_value="qa")
    assert out["attribution"]["mode"] == "inventory" and "no billing data" in out["attribution"]["note"]


def test_requests_per_run_are_bounded(svc):
    _, ce = collect(svc, oct_rows())
    assert len(ce.calls) == 2                              # daily rows + monthly cross-check


# ── collector run / output ────────────────────────────────────────────────


def run(svc, tmp_path, ce, ec2=current_qa, previous=None, identity=lambda: ME):
    return svc.run(cfg(svc), NOW, clients=lambda region: (ce, ec2()), identity=identity,
                   ledger_path=str(tmp_path / "ledger.json"), previous=previous)


def test_a_good_run_writes_fresh_figures_and_the_ledger(svc, tmp_path):
    out = run(svc, tmp_path, FakeCE(oct_rows()))
    assert out["status"] == "ok" and out["schema"] == 2 and out["last_error"] is None
    assert out["fetched_at"] == NOW.isoformat() and out["interval_seconds"] == 8 * 3600
    assert (tmp_path / "ledger.json").exists()


def test_a_failed_run_keeps_the_last_good_figures_and_says_why(svc, tmp_path):
    good = run(svc, tmp_path, FakeCE(oct_rows()))
    good["fetched_at"] = (NOW - timedelta(hours=8)).isoformat()
    out = run(svc, tmp_path, FakeCE([], error=AwsError("AccessDeniedException")), previous=good)
    assert out["status"] == "ok" and out["fetched_at"] == good["fetched_at"]
    assert out["last_error"]["error_code"] == "AccessDeniedException" and out["last_attempt_at"] == NOW.isoformat()


def test_first_run_access_denied_is_unavailable_with_the_fix(svc, tmp_path):
    out = run(svc, tmp_path, FakeCE([], error=AwsError("AccessDeniedException")))
    assert out["status"] == "unavailable" and "ce:GetCostAndUsage" in out["action_needed"]
    assert "periods" not in out


def test_off_ec2_is_unavailable(svc, tmp_path):
    def nowhere():
        raise OSError("no IMDS")
    out = run(svc, tmp_path, FakeCE(oct_rows()), identity=nowhere)
    assert out["status"] == "unavailable" and out["error_code"] == "no_identity"


def test_missing_boto3_is_unavailable(svc, tmp_path):
    def no_boto(_):
        raise ImportError("boto3")
    out = svc.run(cfg(svc), NOW, clients=no_boto, identity=lambda: ME, ledger_path=str(tmp_path / "l.json"))
    assert out["error_code"] == "boto3_missing"


def test_write_is_atomic_and_world_readable(svc, tmp_path):
    out = tmp_path / "m" / "aws-costs.json"
    svc.write({"status": "ok", "fetched_at": NOW.isoformat()}, str(out))
    assert json.loads(out.read_text())["status"] == "ok"
    assert [p.name for p in out.parent.iterdir()] == ["aws-costs.json"]   # no temp file left


# ── backend: reads the file, never AWS ────────────────────────────────────


@pytest.fixture
def cost_file(tmp_path, monkeypatch):
    p = tmp_path / "aws-costs.json"
    monkeypatch.setenv("AWS_COSTS_FILE", str(p))
    return p


def test_no_collector_file_means_not_enabled_never_zero(cost_file):
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "not_installed"


def test_fresh_file_is_served_with_its_age(svc, cost_file, tmp_path):
    payload = run(svc, tmp_path, FakeCE(oct_rows()))
    payload["fetched_at"] = (NOW - timedelta(hours=2)).isoformat()
    cost_file.write_text(json.dumps(payload))
    out = reader.read_costs(NOW)
    assert out["periods"][0]["qa_total_usd"] == payload["periods"][0]["qa_total_usd"]
    assert out["age_seconds"] == 7200 and out["stale"] is False
    assert out["billing_lag_days"] == 1 and out["billing_stale"] is False


def test_old_collector_output_is_flagged_stale(cost_file):
    cost_file.write_text(json.dumps({"schema": 2, "status": "ok", "fetched_at": (NOW - timedelta(hours=30)).isoformat(),
                                     "periods": [{"data_through": "2026-10-21"}]}))
    assert reader.read_costs(NOW)["stale"] is True


def test_billing_data_that_stopped_arriving_is_flagged(cost_file):
    cost_file.write_text(json.dumps({"schema": 2, "status": "ok", "fetched_at": NOW.isoformat(),
                                     "periods": [{"data_through": "2026-10-15"}]}))
    out = reader.read_costs(NOW)
    assert out["stale"] is False and out["billing_stale"] is True and out["billing_lag_days"] == 7


def test_the_old_format_with_account_wide_figures_is_never_served(cost_file):
    cost_file.write_text(json.dumps({"status": "ok", "fetched_at": NOW.isoformat(),
                                     "month_to_date": {"account_total_usd": "181.70",
                                                       "production_lightsail_excluded_usd": "146.10"}}))
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "collector_outdated"
    assert "month_to_date" not in out


def test_collector_unavailable_payload_passes_through(cost_file):
    cost_file.write_text(json.dumps({"schema": 2, "status": "unavailable", "error_code": "AccessDeniedException",
                                     "fetched_at": NOW.isoformat()}))
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "AccessDeniedException"
    assert out["billing_stale"] is False


@pytest.mark.parametrize("body", ["{not json", "[]", '{"status": "ok"}'])
def test_garbage_file_is_unavailable(cost_file, body):
    cost_file.write_text(body)
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "unreadable"


def test_file_the_backend_cannot_read_is_unavailable(cost_file, monkeypatch):
    cost_file.write_text(json.dumps({"schema": 2, "status": "ok", "fetched_at": NOW.isoformat()}))
    real_open = open

    def denied(path, *a, **kw):
        if str(path) == str(cost_file):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", denied)
    out = reader.read_costs(NOW)
    assert out["status"] == "unavailable" and out["error_code"] == "unreadable"


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


def test_the_timer_still_runs_every_eight_hours():
    timer = (_COLLECTOR.parent / "systemd" / "kopyya-aws-costs.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00/8:" in timer
