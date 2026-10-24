#!/usr/bin/env python3
"""AWS cost of the QA (EC2) environment, for Admin → System.

Runs ON THE QA HOST from a systemd timer (scripts/systemd/kopyya-aws-costs.*),
with the instance role's credentials via IMDSv2. The containers never get AWS
credentials or boto3, and the backend only reads the JSON this writes, through
the same read-only mount as host_metrics.py. Installing this is the on-switch —
production (Lightsail) never has it.

What counts as QA, and nothing else:
  tag mode        costs carrying the QA cost-allocation tag (QA_COST_TAG_KEY=VALUE),
                  once that tag has billing data
  inventory mode  EC2-family charges in the region, but per day only up to what
                  the QA resources known that day could have used. QA resources
                  are discovered, not configured: the instance this runs on
                  (instance identity), anything carrying the QA tag, and their
                  attached volumes, addresses and snapshots. Their lifetimes go
                  into a host-only ledger so a replaced instance keeps its
                  history for the billing period.
Anything beyond that (resources that can't be tied to QA, other services,
Lightsail, tax, other regions) is never in the QA total. Charges in the region
that look EC2-related but can't be tied to QA are reported as an attribution
warning with their amount; unrelated services aren't reported at all.

Every figure is UnblendedCost in USD, before credits and refunds. The
projection is actual month-to-date plus the current infrastructure's run rate;
unavailable rather than a guess when a rate can't be derived.

Cost Explorer bills $0.01 per request; one run makes 2–3 (+ pages).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

log = logging.getLogger("aws_costs")

SCHEMA = 2
OUT = os.environ.get("KOPYYA_AWS_COSTS_FILE", "/var/lib/kopyya-metrics/aws-costs.json")
LEDGER = os.environ.get("KOPYYA_AWS_COSTS_LEDGER", "/var/lib/kopyya-aws-costs/ledger.json")
METRIC = "UnblendedCost"
INTERVAL_S = 8 * 3600          # matches kopyya-aws-costs.timer

# Cost Explorer SERVICE values an EC2 workload is billed under. EBS, Elastic IPs
# and most data transfer land in "EC2 - Other"; public IPv4 under VPC.
EC2_FAMILY = [
    "Amazon Elastic Compute Cloud - Compute",
    "EC2 - Other",
    "Amazon Virtual Private Cloud",
    "AmazonCloudWatch",
    "AWS Backup",
    "AWS Data Transfer",
]
CREDIT_TYPES = ["Credit", "Refund"]
NOT_CREDITS = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": CREDIT_TYPES}}}

COMPUTE, VOLUMES, SNAPSHOTS, IPV4, TRANSFER, OTHER = (
    "EC2 Compute", "EBS Volumes", "EBS Snapshots", "Public IPv4", "Data Transfer", "Other verified QA services")
CATEGORIES = [COMPUTE, VOLUMES, SNAPSHOTS, IPV4, TRANSFER, OTHER]

REQUIRED_ACTIONS = ["ce:GetCostAndUsage", "ec2:DescribeInstances", "ec2:DescribeVolumes"]
OPTIONAL_ACTIONS = ["ec2:DescribeAddresses", "ec2:DescribeSnapshots"]

GP3_BASE_IOPS, GP3_BASE_MIBPS = 3000, 125
CAP_SLACK = Decimal("1.005")   # AWS rounds usage; don't call a rounding error "excess"
Q = Decimal("0.01")


@dataclass(frozen=True)
class Config:
    region: str | None
    tag_key: str | None
    tag_value: str | None

    @classmethod
    def from_env(cls) -> "Config":
        for legacy in ("QA_COST_INSTANCE_ID", "QA_COST_ALLOCATE_REGIONAL_EC2"):
            if os.environ.get(legacy):
                log.info("%s is ignored: QA resources are discovered", legacy)
        return cls(
            region=os.environ.get("QA_COST_REGION") or None,
            tag_key=os.environ.get("QA_COST_TAG_KEY") or None,
            tag_value=os.environ.get("QA_COST_TAG_VALUE") or None,
        )

    @property
    def tagged(self) -> bool:
        return bool(self.tag_key and self.tag_value)


# ── usage types ───────────────────────────────────────────────────────────


_REGION_PREFIX = re.compile(r"^[A-Z]{2,4}\d{0,2}-(?=[A-Za-z])")
_XREGION = re.compile(r"^(?:([A-Z]{2,4}\d{0,2})-)?([A-Z]{2,4}\d{0,2})-AWS-(In|Out)-Bytes$")
# Billing codes for the regions cross-region transfer shows up with.
REGION_CODES = {
    "USE1": "us-east-1", "USE2": "us-east-2", "USW1": "us-west-1", "USW2": "us-west-2",
    "CAN1": "ca-central-1", "EU": "eu-west-1", "EUW2": "eu-west-2", "EUW3": "eu-west-3",
    "EUC1": "eu-central-1", "EUN1": "eu-north-1", "EUS1": "eu-south-1", "APN1": "ap-northeast-1",
    "APN2": "ap-northeast-2", "APN3": "ap-northeast-3", "APS1": "ap-southeast-1",
    "APS2": "ap-southeast-2", "APS3": "ap-south-1", "SAE1": "sa-east-1", "MES1": "me-south-1",
    "AFS1": "af-south-1",
}


def strip_region(usage_type: str) -> str:
    """Usage types carry a region prefix ("USE2-BoxUsage:…"), except us-east-1."""
    return _REGION_PREFIX.sub("", usage_type)


def categorize(service: str, usage_type: str) -> tuple[str, str]:
    """(category, how it's charged) for one Cost Explorer SERVICE/USAGE_TYPE."""
    u = strip_region(usage_type)
    if any(k in u for k in ("BoxUsage", "SpotUsage", "DedicatedUsage", "HostUsage")):
        return COMPUTE, "Per instance-hour"
    if "EBS:Snapshot" in u:
        return SNAPSHOTS, "Per GB-month stored"
    if u.startswith("EBS:"):
        if "IOPS" in u:
            return VOLUMES, "Per provisioned IOPS-month"
        if "Throughput" in u:
            return VOLUMES, "Per provisioned MiB/s-month"
        return VOLUMES, "Per provisioned GB-month"
    if "PublicIPv4" in u or "ElasticIP" in u:
        return IPV4, "Per address-hour"
    if _is_transfer(usage_type):
        return TRANSFER, "Per GB"
    return OTHER, "See usage type"


def _is_transfer(usage_type: str) -> bool:
    u = strip_region(usage_type)
    return "DataTransfer" in u or bool(_XREGION.match(usage_type)) or u.endswith("-Bytes")


def transfer_direction(usage_type: str) -> str:
    u = strip_region(usage_type)
    m = _XREGION.match(usage_type)
    if m:
        _, other, way = m.groups()      # [SRC-]DST-AWS-Out: DST is the far end
        name = REGION_CODES.get(other, other)
        return f"Out to AWS {name}" if way == "Out" else f"In from AWS {name}"
    if "Regional" in u:
        return "Within region (between AZs)"
    if u.endswith("Out-Bytes"):
        return "Out to internet"
    if u.endswith("In-Bytes"):
        return "In from internet"
    return u


def transfer_bucket(usage_type: str) -> tuple[str, str | None]:
    """Breakdown row for a transfer usage type, and the far region if any.
    Cross-region traffic shows up as one usage type per region pair; one row
    per direction is what's readable."""
    m = _XREGION.match(usage_type)
    if m:
        _, other, way = m.groups()
        return ("Out to other AWS regions" if way == "Out" else "In from other AWS regions"), \
            REGION_CODES.get(other, other)
    return transfer_direction(usage_type), None


# ── discovery ─────────────────────────────────────────────────────────────


def _utc(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _tags(obj: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in obj.get("Tags") or []}


def _error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return (resp.get("Error") or {}).get("Code") or type(exc).__name__
    return type(exc).__name__


_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


@dataclass
class Inventory:
    """QA resources as AWS reports them right now, plus what couldn't be read."""
    instances: list[dict] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)
    addresses: list[dict] = field(default_factory=list)   # QA public IPv4 not on a QA instance
    snapshots: list[dict] | None = None                   # None = couldn't read
    other_instances: list[dict] = field(default_factory=list)
    other_snapshots: list[dict] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def discover(ec2: Any, identity: dict, cfg: Config, known_volume_ids: set[str]) -> Inventory:
    """QA = the instance this runs on, anything carrying the QA tag, and what's
    attached to or made from them. DescribeInstances/Volumes are required;
    addresses and snapshots are optional and reported when they can't be read."""
    inv = Inventory()
    is_qa_tag = (lambda t: cfg.tagged and t.get(cfg.tag_key) == cfg.tag_value)

    qa_ids: set[str] = set()
    for res in ec2.describe_instances()["Reservations"]:
        for i in res.get("Instances") or []:
            state = i["State"]["Name"]
            if state in ("terminated", "shutting-down"):
                continue
            # LaunchTime resets on every start; the root volume's attach time doesn't
            attach = [_utc(b["Ebs"]["AttachTime"]) for b in i.get("BlockDeviceMappings") or [] if b.get("Ebs")]
            first = min(attach) if attach else _utc(i["LaunchTime"])
            rec = {"id": i["InstanceId"], "type": i["InstanceType"], "state": state, "first_seen": first,
                   "public_ipv4": bool(i.get("PublicIpAddress"))}
            if i["InstanceId"] == identity["instance_id"] or is_qa_tag(_tags(i)):
                qa_ids.add(i["InstanceId"])
                inv.instances.append(rec)
            else:
                inv.other_instances.append(rec)

    for v in ec2.describe_volumes()["Volumes"]:
        attached = {a["InstanceId"] for a in v.get("Attachments") or []}
        if attached & qa_ids or is_qa_tag(_tags(v)):
            inv.volumes.append({"id": v["VolumeId"], "type": v["VolumeType"], "size": int(v["Size"]),
                                "iops": int(v.get("Iops") or 0), "throughput": int(v.get("Throughput") or 0),
                                "first_seen": _utc(v["CreateTime"])})
    volume_ids = known_volume_ids | {v["id"] for v in inv.volumes}

    try:
        for a in ec2.describe_addresses()["Addresses"]:
            if a.get("InstanceId") in qa_ids:
                continue                       # counted with its instance
            if is_qa_tag(_tags(a)):
                inv.addresses.append({"id": a.get("AllocationId") or a.get("PublicIp")})
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in _DENIED:
            raise
        inv.unreadable.append("ec2:DescribeAddresses")

    try:
        snaps = ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in _DENIED:
            raise
        inv.unreadable.append("ec2:DescribeSnapshots")
    else:
        inv.snapshots = []
        for s in snaps:
            rec = {"id": s["SnapshotId"], "first_seen": _utc(s["StartTime"])}
            (inv.snapshots if s.get("VolumeId") in volume_ids or is_qa_tag(_tags(s)) else inv.other_snapshots).append(rec)
    return inv


def imds_identity(timeout: float = 2.0) -> dict:
    """This host's instance id and region (IMDSv2). Host only — the containers'
    hop limit keeps them off IMDS."""
    base = "http://169.254.169.254/latest/"
    tok = urllib.request.urlopen(urllib.request.Request(
        base + "api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}),
        timeout=timeout).read().decode()
    doc = json.loads(urllib.request.urlopen(urllib.request.Request(
        base + "dynamic/instance-identity/document", headers={"X-aws-ec2-metadata-token": tok}),
        timeout=timeout).read())
    return {"instance_id": doc["instanceId"], "region": doc["region"]}


# ── ledger: QA resource lifetimes, kept across runs ───────────────────────


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def load_ledger(path: str = LEDGER) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("resources"), dict) else {"resources": {}}
    except (OSError, ValueError):
        return {"resources": {}}


def save_ledger(ledger: dict, path: str = LEDGER) -> None:
    _atomic_write(ledger, path, mode=0o600)


def update_ledger(ledger: dict, inv: Inventory, now: datetime) -> dict:
    """One entry per resource configuration (an instance resized, a volume grown
    → a new entry), so each day is checked against what existed then. Entries
    not seen this run keep their last_seen: that's how a replaced instance's
    hours stay QA for the rest of the billing period."""
    res = ledger.setdefault("resources", {})

    def seen(key: str, first: datetime, **attrs: Any) -> None:
        e = res.get(key)
        if e is None:
            res[key] = {**attrs, "first_seen": _iso(first), "last_seen": _iso(now)}
        else:
            e["first_seen"] = _iso(min(_utc(e["first_seen"]), first))
            e["last_seen"] = _iso(now)
            e.update(attrs)

    for i in inv.instances:
        # a resize needs a stop, so the new size starts no earlier than this run
        prior = [e for k, e in res.items() if e.get("kind") == "instance" and e.get("id") == i["id"]
                 and e.get("type") != i["type"]]
        first = max([i["first_seen"]] + [_utc(e["last_seen"]) for e in prior])
        seen(f"instance:{i['id']}:{i['type']}", first, kind="instance", id=i["id"], type=i["type"],
             public_ipv4=i["public_ipv4"] or res.get(f"instance:{i['id']}:{i['type']}", {}).get("public_ipv4", False))
    for v in inv.volumes:
        seen(f"volume:{v['id']}:{v['type']}:{v['size']}:{v['iops']}:{v['throughput']}", v["first_seen"],
             kind="volume", id=v["id"], type=v["type"], size=v["size"], iops=v["iops"], throughput=v["throughput"])
    for a in inv.addresses:
        seen(f"address:{a['id']}", now, kind="address", id=a["id"])
    for s in inv.snapshots or []:
        seen(f"snapshot:{s['id']}", s["first_seen"], kind="snapshot", id=s["id"])
    for s in inv.other_snapshots:
        seen(f"other_snapshot:{s['id']}", s["first_seen"], kind="other_snapshot", id=s["id"])
    for i in inv.other_instances:
        seen(f"other_instance:{i['id']}", i["first_seen"], kind="other_instance", id=i["id"])
    ledger["updated_at"] = _iso(now)
    return ledger


def _windows(ledger: dict, kind: str, now: datetime) -> list[tuple[dict, datetime, datetime]]:
    """(entry, start, end). A resource not seen in the latest run may have lived
    up to one collector interval longer — capacity is an upper bound."""
    latest = ledger.get("updated_at")
    out = []
    for e in ledger.get("resources", {}).values():
        if e.get("kind") != kind:
            continue
        end = _utc(e["last_seen"])
        if latest and e["last_seen"] != latest:
            end = min(now, end + timedelta(seconds=INTERVAL_S))
        out.append((e, _utc(e["first_seen"]), end))
    return out


def _overlap_h(start: datetime, end: datetime, day: date) -> Decimal:
    d0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=1)
    s = max(start, d0)
    e = min(end, d1)
    return Decimal(str(round(max(0.0, (e - s).total_seconds()) / 3600, 6)))


# ── attribution ───────────────────────────────────────────────────────────


@dataclass
class Row:
    day: date
    service: str
    usage_type: str
    cost: Decimal
    qty: Decimal
    unit: str


def _capacity(group: str, day: date, ledger: dict, now: datetime) -> Decimal | None:
    """Most a QA resource set could have billed under this capacity group that
    day; None = no limit (verified by inventory instead)."""
    dim = Decimal(calendar.monthrange(day.year, day.month)[1]) * 24
    kind, _, detail = group.partition(":")
    if kind == "compute":
        return sum((_overlap_h(s, e, day) for x, s, e in _windows(ledger, "instance", now) if x["type"] == detail),
                   Decimal(0))
    if kind in ("volume", "iops", "throughput"):
        total = Decimal(0)
        for x, s, e in _windows(ledger, "volume", now):
            if x["type"] != detail:
                continue
            if kind == "volume":
                amount = Decimal(x["size"])
            elif kind == "iops":
                amount = Decimal(max(0, x["iops"] - (GP3_BASE_IOPS if detail == "gp3" else 0)))
            else:
                amount = Decimal(max(0, x["throughput"] - GP3_BASE_MIBPS))
            total += amount * _overlap_h(s, e, day) / dim
        return total
    if kind == "ipv4":
        hours = sum((_overlap_h(s, e, day) for x, s, e in _windows(ledger, "instance", now) if x.get("public_ipv4")),
                    Decimal(0))
        return hours + sum((_overlap_h(s, e, day) for _, s, e in _windows(ledger, "address", now)), Decimal(0))
    raise ValueError(group)


def _group(usage_type: str) -> str | None:
    u = strip_region(usage_type)
    if any(k in u for k in ("BoxUsage", "SpotUsage", "DedicatedUsage", "HostUsage")):
        return "compute:" + u.split(":", 1)[1] if ":" in u else None
    m = re.match(r"EBS:VolumeUsage(?:\.(.+))?$", u)
    if m:
        return "volume:" + (m.group(1) or "standard")
    m = re.match(r"EBS:VolumeP-IOPS\.(.+)$", u)
    if m:
        return "iops:" + m.group(1)
    m = re.match(r"EBS:VolumeP-Throughput\.(.+)$", u)
    if m:
        return "throughput:" + m.group(1)
    if "PublicIPv4" in u or "ElasticIP" in u:
        return "ipv4"
    return None


def attribute(rows: list[Row], ledger: dict, now: datetime, snapshots_readable: bool
              ) -> tuple[list[tuple[Row, Decimal]], list[tuple[Row, Decimal, str]]]:
    """Split each billed row into (QA part) and (excluded part, reason).
    The two always add back up to the row — nothing dropped, nothing doubled."""
    qa: list[tuple[Row, Decimal]] = []
    excluded: list[tuple[Row, Decimal, str]] = []
    by_day: dict[date, list[Row]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)

    for day, day_rows in sorted(by_day.items()):
        compute_excess = False
        transfer, snapshots = [], []
        groups: dict[str, list[Row]] = {}
        for r in day_rows:
            cat, _ = categorize(r.service, r.usage_type)
            g = _group(r.usage_type)
            if g:
                groups.setdefault(g, []).append(r)
            elif cat == TRANSFER:
                transfer.append(r)
            elif cat == SNAPSHOTS:
                snapshots.append(r)
            else:
                excluded.append((r, r.cost, "unverified_service"))

        for g, grows in groups.items():
            cap = _capacity(g, day, ledger, now)
            cap = cap * CAP_SLACK + Decimal("0.01") if cap > 0 else Decimal(0)
            # InUse before Idle for IPv4; otherwise order doesn't matter
            for r in sorted(grows, key=lambda x: ("Idle" in x.usage_type, x.usage_type)):
                if r.qty <= 0:
                    share = Decimal(1) if cap > Decimal("0.01") else Decimal(0)
                else:
                    share = min(Decimal(1), max(Decimal(0), cap / r.qty))
                cap = max(Decimal(0), cap - r.qty)
                part = r.cost * share
                if part:
                    qa.append((r, part))
                if r.cost - part:
                    excluded.append((r, r.cost - part, "no_qa_resource"))
                    compute_excess |= g.startswith("compute:")

        # Snapshots have no billed size per snapshot: all-or-nothing, and only
        # on days every snapshot alive in the region is a QA one.
        qa_snap = any(_overlap_h(s, e, day) for _, s, e in _windows(ledger, "snapshot", now))
        other_snap = any(_overlap_h(s, e, day) for _, s, e in _windows(ledger, "other_snapshot", now))
        for r in snapshots:
            if snapshots_readable and qa_snap and not other_snap:
                qa.append((r, r.cost))
            else:
                excluded.append((r, r.cost, "snapshots_unverified" if not snapshots_readable else "no_qa_resource"))

        # Transfer can't be traced to a resource: QA's only when a QA instance
        # was up that day and no other instance could have sent it.
        qa_up = any(_overlap_h(s, e, day) for _, s, e in _windows(ledger, "instance", now))
        others_up = any(_overlap_h(s, e, day) for _, s, e in _windows(ledger, "other_instance", now))
        for r in transfer:
            if qa_up and not others_up and not compute_excess:
                qa.append((r, r.cost))
            else:
                excluded.append((r, r.cost, "transfer_ambiguous"))
    return qa, excluded


# ── Cost Explorer ─────────────────────────────────────────────────────────


def _amount(m: dict, metric: str = METRIC) -> Decimal:
    return Decimal(str(((m or {}).get(metric) or {}).get("Amount") or "0"))


def _paged(ce: Any, **kw) -> list[dict]:
    out, token = [], None
    while True:
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        out.extend(resp.get("ResultsByTime") or [])
        token = resp.get("NextPageToken")
        if not token:
            return out


def _rows(results: list[dict]) -> list[Row]:
    out = []
    for r in results:
        day = date.fromisoformat(r["TimePeriod"]["Start"])
        for g in r.get("Groups") or []:
            svc, ut = (g["Keys"] + [""])[:2]
            m = g.get("Metrics") or {}
            usage = m.get("UsageQuantity") or {}
            cost = _amount(m)
            qty = Decimal(str(usage.get("Amount") or "0"))
            if cost or qty:
                out.append(Row(day, svc, ut, cost, qty, usage.get("Unit") or ""))
    return out


def fetch_rows(ce: Any, region: str, start: date, end: date, tag: tuple[str, str] | None = None) -> list[Row]:
    scope: list[dict] = [{"Dimensions": {"Key": "REGION", "Values": [region]}},
                         {"Dimensions": {"Key": "SERVICE", "Values": EC2_FAMILY}}, NOT_CREDITS]
    if tag:
        scope.append({"Tags": {"Key": tag[0], "Values": [tag[1]]}})
    return _rows(_paged(ce, TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                        Granularity="DAILY", Metrics=[METRIC, "UsageQuantity"], Filter={"And": scope},
                        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]))


def monthly_total(ce: Any, region: str, start: date, end: date) -> Decimal:
    """Independent check: the same scope summed by Cost Explorer itself."""
    res = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="MONTHLY", Metrics=[METRIC],
        Filter={"And": [{"Dimensions": {"Key": "REGION", "Values": [region]}},
                        {"Dimensions": {"Key": "SERVICE", "Values": EC2_FAMILY}}, NOT_CREDITS]})
    return sum((_amount(r.get("Total") or {}) for r in res.get("ResultsByTime") or []), Decimal(0))


# ── payload ───────────────────────────────────────────────────────────────


def _money(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(Q))


def _fmt_qty(q: Decimal) -> str:
    q = q.quantize(Decimal("0.001")) if q >= Decimal("0.001") else q.normalize()
    return f"{q.normalize():f}"


def _categories(qa: list[tuple[Row, Decimal]]) -> list[dict]:
    items: dict[tuple[str, str, str], dict] = {}
    for r, part in qa:
        cat, charge = categorize(r.service, r.usage_type)
        region = None
        if cat == TRANSFER:
            label, region = transfer_bucket(r.usage_type)
        else:
            label = strip_region(r.usage_type)
        it = items.setdefault((cat, label, r.unit), {
            "category": cat, "description": label, "usage_types": set(), "regions": set(),
            "unit": r.unit, "charge_type": charge, "cost": Decimal(0), "qty": Decimal(0)})
        it["usage_types"].add(strip_region(r.usage_type))
        if region:
            it["regions"].add(region)
        it["cost"] += part
        # the QA share of the quantity, at the row's own unit price
        it["qty"] += r.qty * (part / r.cost) if r.cost else r.qty
    out = []
    for cat in CATEGORIES:
        rows = sorted((v for (c, _, _), v in items.items() if c == cat), key=lambda v: (-v["cost"], -v["qty"]))
        if not rows:
            continue
        cost = sum((v["cost"] for v in rows), Decimal(0))
        units = {v["unit"] for v in rows}
        out.append({
            "category": cat,
            "cost_usd": _money(cost),
            # one unit only — never add hours to GB-months
            "quantity": _fmt_qty(sum((v["qty"] for v in rows), Decimal(0))) if len(units) == 1 else None,
            "unit": units.pop() if len(units) == 1 else None,
            "items": [{
                "usage_type": ", ".join(sorted(v["usage_types"])),
                "description": v["description"],
                "regions": sorted(v["regions"]),
                "quantity": _fmt_qty(v["qty"]), "unit": v["unit"],
                "cost_usd": str(v["cost"].quantize(Decimal("0.0001"))),
                "charge_type": v["charge_type"],
            } for v in rows],
            "_cost": cost,
        })
    return out


_REASONS = {
    "no_qa_resource": "billed usage beyond what the known QA resources could have used that day "
                      "(e.g. instances or volumes that no longer exist and were never tied to QA)",
    "snapshots_unverified": "EBS snapshots can't be tied to QA volumes without ec2:DescribeSnapshots",
    "transfer_ambiguous": "data transfer on days other EC2 resources were running, so it can't be traced to QA",
    "unverified_service": "EC2-related services that can't be tied to a QA resource without the QA cost-allocation tag",
    "untagged": "EC2-family charges in the region that don't carry the QA cost-allocation tag",
}


def _warnings(excluded: list[tuple[Row, Decimal, str]], label: str) -> list[dict]:
    by: dict[str, dict] = {}
    for r, amt, reason in excluded:
        w = by.setdefault(reason, {"code": reason, "period": label, "amount": Decimal(0), "first": r.day, "last": r.day,
                                   "categories": set()})
        w["amount"] += amt
        w["first"], w["last"] = min(w["first"], r.day), max(w["last"], r.day)
        w["categories"].add(categorize(r.service, r.usage_type)[0])
    out = []
    for w in by.values():
        if w["amount"] < Decimal("0.005"):
            continue
        out.append({"code": w["code"], "period": w["period"], "excluded_usd": _money(w["amount"]),
                    "from": w["first"].isoformat(), "to": w["last"].isoformat(),
                    "categories": sorted(w["categories"], key=CATEGORIES.index), "message": _REASONS[w["code"]]})
    return sorted(out, key=lambda w: -Decimal(w["excluded_usd"]))


def _period(month_start: date, end: date, qa: list, excluded: list, all_rows: list[Row],
            ce_total: Decimal | None) -> dict:
    in_p = (lambda d: month_start <= d < end)
    p_qa = [(r, a) for r, a in qa if in_p(r.day)]
    p_ex = [(r, a, why) for r, a, why in excluded if in_p(r.day)]
    cats = _categories(p_qa)
    total = sum((Decimal(c["cost_usd"]) for c in cats), Decimal(0))   # = sum of the rows shown
    billed = sum((r.cost for r in all_rows if in_p(r.day)), Decimal(0))
    attributed = sum((a for _, a in p_qa), Decimal(0))
    excl = sum((a for _, a, _ in p_ex), Decimal(0))
    days = {}
    d = month_start
    while d < end:
        days[d] = [Decimal(0), Decimal(0)]
        d += timedelta(days=1)
    for r, a in p_qa:
        days[r.day][0] += a
    for r, a, _ in p_ex:
        days[r.day][1] += a
    billed_days = [r.day for r in all_rows if in_p(r.day) and r.cost > 0]
    for c in cats:
        del c["_cost"]
    label = month_start.strftime("%Y-%m")
    return {
        "month": label,
        "start": month_start.isoformat(),
        "data_through": max(billed_days).isoformat() if billed_days else None,
        "qa_total_usd": _money(total),
        "excluded_usd": _money(excl),
        "categories": cats,
        "daily": [{"date": k.isoformat(), "cost_usd": _money(v[0]), "excluded_usd": _money(v[1])}
                  for k, v in sorted(days.items())],
        "warnings": _warnings(p_ex, label),
        "reconciliation": {
            "billed_in_scope_usd": str(billed.quantize(Decimal("0.0001"))),
            "attributed_usd": str(attributed.quantize(Decimal("0.0001"))),
            "excluded_usd": str(excl.quantize(Decimal("0.0001"))),
            "balanced": abs(billed - attributed - excl) < Decimal("0.0001"),
            "cost_explorer_monthly_usd": None if ce_total is None else str(ce_total.quantize(Decimal("0.0001"))),
            "matches_cost_explorer": None if ce_total is None else abs(ce_total - billed) <= Q,
        },
    }


def _rate(qa: list[tuple[Row, Decimal]], usage: str) -> Decimal | None:
    """Unit price AWS actually charged for this usage type (cost / quantity)."""
    cost = qty = Decimal(0)
    for r, part in qa:
        if strip_region(r.usage_type) == usage and r.qty > 0 and r.cost > 0:
            cost += r.cost
            qty += r.qty
    return cost / qty if qty else None


def run_rate(inv: Inventory, qa: list[tuple[Row, Decimal]], current: dict, today: date) -> dict:
    """Daily cost of the infrastructure as it stands now. Fixed parts from the
    discovered resources × the unit prices AWS billed; variable parts from
    recent days. Unavailable — not a guess — when a price is missing."""
    dim = Decimal(calendar.monthrange(today.year, today.month)[1])
    comps: list[dict] = []
    missing: list[str] = []

    def add(cat: str, basis: str, daily: Decimal | None, need: str | None = None) -> None:
        if daily is None:
            missing.append(need or basis)
            return
        comps.append({"category": cat, "basis": basis, "daily_usd": str(daily.quantize(Decimal("0.0001")))})

    running = [i for i in inv.instances if i["state"] == "running"]
    for i in inv.instances:
        if i["state"] != "running":
            add(COMPUTE, f"{i['type']} {i['id']} is {i['state']} — no compute charge", Decimal(0))
    for i in running:
        r = _rate(qa, f"BoxUsage:{i['type']}")
        add(COMPUTE, f"{i['type']} × 24 h", None if r is None else r * 24, f"a billed hourly price for {i['type']}")

    for v in inv.volumes:
        r = _rate(qa, f"EBS:VolumeUsage.{v['type']}") if v["type"] != "standard" else _rate(qa, "EBS:VolumeUsage")
        add(VOLUMES, f"{v['size']} GiB {v['type']}", None if r is None else Decimal(v["size"]) * r / dim,
            f"a billed GB-month price for {v['type']}")
        extra_iops = v["iops"] - (GP3_BASE_IOPS if v["type"] == "gp3" else 0)
        if v["type"] in ("gp3", "io1", "io2") and extra_iops > 0:
            r = _rate(qa, f"EBS:VolumeP-IOPS.{v['type']}")
            add(VOLUMES, f"{extra_iops} provisioned IOPS", None if r is None else Decimal(extra_iops) * r / dim,
                f"a billed IOPS price for {v['type']}")
        if v["type"] == "gp3" and v["throughput"] > GP3_BASE_MIBPS:
            extra = v["throughput"] - GP3_BASE_MIBPS
            r = _rate(qa, "EBS:VolumeP-Throughput.gp3")
            add(VOLUMES, f"{extra} MiB/s provisioned throughput", None if r is None else Decimal(extra) * r / dim,
                "a billed throughput price for gp3")

    n_ip = sum(1 for i in inv.instances if i["public_ipv4"]) + len(inv.addresses)
    if n_ip:
        r = _rate(qa, "PublicIPv4:InUseAddress") or _rate(qa, "PublicIPv4:IdleAddress")
        add(IPV4, f"{n_ip} address{'es' if n_ip > 1 else ''} × 24 h", None if r is None else r * n_ip * 24,
            "a billed public IPv4 price")

    through = current.get("data_through")
    covered = [d for d in current["daily"] if through and d["date"] <= through]
    by_cat_day: dict[tuple[str, str], Decimal] = {}
    for r, part in qa:
        k = (categorize(r.service, r.usage_type)[0], r.day.isoformat())
        by_cat_day[k] = by_cat_day.get(k, Decimal(0)) + part
    if covered:
        last = covered[-1]["date"]
        snap = by_cat_day.get((SNAPSHOTS, last), Decimal(0))
        if snap:
            add(SNAPSHOTS, f"stored snapshots, as billed on {last}", snap)
        other = by_cat_day.get((OTHER, last), Decimal(0))
        if other:
            add(OTHER, f"as billed on {last}", other)
        recent = [d["date"] for d in covered[-7:]]
        xfer = [by_cat_day.get((TRANSFER, d), Decimal(0)) for d in recent]
        if any(xfer):
            add(TRANSFER, f"average of the last {len(recent)} billed days", sum(xfer, Decimal(0)) / len(recent))

    if missing:
        return {"available": False, "reason": "No billed price yet for " + "; ".join(missing) + ".",
                "daily_usd": None, "components": comps}
    daily = sum((Decimal(c["daily_usd"]) for c in comps), Decimal(0))
    return {"available": True, "reason": None, "daily_usd": _money(daily), "components": comps}


def projection(current: dict, rate: dict, today: date) -> dict:
    """Actual through the last billed day + run rate for the rest of the month."""
    if not rate["available"]:
        return {"month_usd": None, "method": None, "reason": rate["reason"]}
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1]) + timedelta(days=1)
    through = current["data_through"]
    start = date.fromisoformat(through) + timedelta(days=1) if through else date.fromisoformat(current["start"])
    remaining = max(0, (month_end - start).days)
    actual = Decimal(current["qa_total_usd"])
    return {"month_usd": _money(actual + Decimal(rate["daily_usd"]) * remaining),
            "method": "actual_plus_run_rate", "actual_usd": current["qa_total_usd"],
            "remaining_days": remaining, "reason": None}


def collect(ce: Any, ec2: Any, cfg: Config, identity: dict, ledger: dict, now: datetime) -> dict[str, Any]:
    today = now.date()
    region = cfg.region or identity["region"]
    month_start = today.replace(day=1)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)

    inv = discover(ec2, identity, cfg, {e["id"] for e in ledger.get("resources", {}).values()
                                        if e.get("kind") == "volume"})
    update_ledger(ledger, inv, now)

    # through yesterday: today's partial day would read low against the run rate
    rows = fetch_rows(ce, region, prev_start, today)
    mode, mode_note = "inventory", None
    if cfg.tagged:
        tagged = fetch_rows(ce, region, prev_start, today, (cfg.tag_key, cfg.tag_value))
        if tagged:
            mode = "tag"
            keyed = {}
            for r in tagged:
                keyed[(r.day, r.service, r.usage_type)] = r
            qa = [(r, r.cost) for r in tagged]
            excluded = []
            for r in rows:
                t = keyed.get((r.day, r.service, r.usage_type))
                rest = r.cost - (t.cost if t else Decimal(0))
                if rest > 0:
                    excluded.append((r, rest, "untagged"))
        else:
            mode_note = f"Tag {cfg.tag_key}={cfg.tag_value} has no billing data yet; using discovered resources."
    if mode == "inventory":
        qa, excluded = attribute(rows, ledger, now, inv.snapshots is not None)

    ce_total = monthly_total(ce, region, month_start, today) if today > month_start else Decimal(0)
    current = _period(month_start, today, qa, excluded, rows, ce_total)
    previous = _period(prev_start, month_start, qa, excluded, rows, None)
    rate = run_rate(inv, qa, current, today)

    warnings = current["warnings"] + previous["warnings"]
    return {
        "schema": SCHEMA,
        "status": "ok",
        "currency": "USD",
        "metric": METRIC,
        "basis": "before credits and refunds",
        "region": region,
        "environment": {
            "instances": [{"id": i["id"], "type": i["type"], "state": i["state"], "public_ipv4": i["public_ipv4"]}
                          for i in inv.instances],
            "volumes": [{"id": v["id"], "type": v["type"], "size_gib": v["size"], "iops": v["iops"],
                         "throughput_mibps": v["throughput"]} for v in inv.volumes],
            "public_ipv4": sum(1 for i in inv.instances if i["public_ipv4"]) + len(inv.addresses),
            "snapshots": None if inv.snapshots is None else len(inv.snapshots),
            "discovered_via": "instance identity" + (f" + tag {cfg.tag_key}={cfg.tag_value}" if cfg.tagged else ""),
            "unreadable": inv.unreadable,
        },
        "attribution": {
            "mode": mode,
            "note": mode_note,
            "complete": not warnings and not inv.unreadable,
            "warnings": warnings,
        },
        "periods": [current, previous],
        "run_rate": rate,
        "projection": projection(current, rate, today),
    }


# ── run + output ──────────────────────────────────────────────────────────


_HINTS = {
    "AccessDenied": "The QA instance role needs " + ", ".join(REQUIRED_ACTIONS) + " (read-only).",
    "AccessDeniedException": "The QA instance role needs " + ", ".join(REQUIRED_ACTIONS) + " (read-only).",
    "UnauthorizedOperation": "The QA instance role needs " + ", ".join(REQUIRED_ACTIONS) + " (read-only).",
    "DataUnavailableException": "Cost Explorer has no data yet — it can take up to 24h after it is first enabled.",
    "OptInRequired": "Cost Explorer isn't enabled for this account (Billing console → Cost Explorer).",
    "NoCredentialsError": "The collector on the QA host can't reach the EC2 instance role.",
    "no_identity": "Run the collector on the QA EC2 host itself (it identifies QA from instance metadata).",
    "boto3_missing": "Install python3-boto3 on the QA host.",
}


def unavailable(reason: str, code: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "status": "unavailable", "reason": reason, "error_code": code,
            "action_needed": _HINTS.get(code, "AWS returned an error; see `journalctl -u kopyya-aws-costs`."),
            "required_permissions": REQUIRED_ACTIONS}


def run(cfg: Config, now: datetime, clients: Callable | None = None, identity: Callable | None = None,
        ledger_path: str = LEDGER, previous: dict | None = None) -> dict[str, Any]:
    """A failed run keeps the last good figures (marked with the error) instead
    of blanking the page; the backend calls them stale once they age out."""
    payload: dict[str, Any]
    try:
        ident = (identity or imds_identity)()
    except Exception as exc:  # noqa: BLE001
        log.warning("instance identity unavailable: %s", type(exc).__name__)
        payload = unavailable("Not running on an EC2 instance (no instance identity)", "no_identity")
    else:
        try:
            ce, ec2 = (clients or _default_clients)(cfg.region or ident["region"])
        except ImportError:
            payload = unavailable("boto3 not installed on the host", "boto3_missing")
        else:
            ledger = load_ledger(ledger_path)
            try:
                payload = collect(ce, ec2, cfg, ident, ledger, now)
            except Exception as exc:  # noqa: BLE001
                code = _error_code(exc)
                log.warning("cost collection failed: %s", code)
                payload = unavailable(f"AWS query failed ({code})", code)
            else:
                save_ledger(ledger, ledger_path)
    if payload["status"] == "unavailable" and previous and previous.get("status") == "ok" \
            and previous.get("schema") == SCHEMA:
        kept = dict(previous)
        kept["last_attempt_at"] = now.isoformat()
        kept["last_error"] = {"error_code": payload["error_code"], "reason": payload["reason"],
                              "action_needed": payload["action_needed"]}
        return kept
    payload["fetched_at"] = now.isoformat()
    payload["last_attempt_at"] = now.isoformat()
    payload["last_error"] = None
    payload["interval_seconds"] = INTERVAL_S
    return payload


def _default_clients(region: str) -> tuple[Any, Any]:
    import boto3  # noqa: PLC0415

    # Cost Explorer is served from us-east-1 regardless of where resources run.
    return boto3.client("ce", region_name="us-east-1"), boto3.client("ec2", region_name=region)


def _atomic_write(obj: dict, out: str, mode: int = 0o644) -> None:
    d = os.path.dirname(out)
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".aws-costs.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.chmod(tmp, mode)
    os.replace(tmp, out)


def write(payload: dict[str, Any], out: str = OUT) -> None:
    _atomic_write(payload, out)


def _read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload = run(Config.from_env(), datetime.now(timezone.utc), previous=_read(OUT))
    write(payload)
    cur = (payload.get("periods") or [{}])[0]
    log.info("status=%s qa_mtd=%s excluded=%s through=%s error=%s", payload["status"], cur.get("qa_total_usd"),
             cur.get("excluded_usd"), cur.get("data_through"), (payload.get("last_error") or {}).get("error_code"))
    return 0 if payload["status"] == "ok" and not payload.get("last_error") else 1


if __name__ == "__main__":
    sys.exit(main())
