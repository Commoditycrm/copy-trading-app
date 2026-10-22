#!/usr/bin/env python3
"""AWS cost of the QA (EC2) environment, for Admin → System.

Runs ON THE QA HOST from a systemd timer (scripts/systemd/kopyya-aws-costs.*),
with the instance role's credentials via IMDSv2. The containers never get AWS
credentials or boto3: with the instance's IMDS hop limit at 1 they can't reach
the role at all, and the backend only reads the JSON this writes, through the
same read-only mount as host_metrics.py. Installing this is the on-switch —
production (Lightsail) never has it.

Attribution, never mixed up:
  direct       — costs carrying the QA cost-allocation tag (QA_COST_TAG_KEY=VALUE)
  allocated    — EC2-family charges in QA_COST_REGION not already counted as
                 direct, credited to QA only when QA_COST_ALLOCATE_REGIONAL_EC2=true
                 (sound while QA is the account's only EC2 workload in that region)
  unattributed — everything else in the account, incl. Production (Lightsail),
                 shown separately and never added to the QA total
Every figure is UnblendedCost in USD, before credits and refunds (the Cost
Explorer console with Charge type excluding Credit and Refund); credits are
reported separately. Unavailable is reported as such, never 0.

Cost Explorer bills $0.01 per request; one run makes 4–5, three runs a day.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

log = logging.getLogger("aws_costs")

OUT = os.environ.get("KOPYYA_AWS_COSTS_FILE", "/var/lib/kopyya-metrics/aws-costs.json")
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
PRODUCTION_SERVICES = {"Amazon Lightsail"}
CREDIT_TYPES = ["Credit", "Refund"]
NOT_CREDITS = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": CREDIT_TYPES}}}

REQUIRED_ACTIONS = ["ce:GetCostAndUsage", "ce:GetCostForecast", "ec2:DescribeInstances"]


@dataclass(frozen=True)
class Config:
    region: str
    instance_id: str | None
    tag_key: str | None
    tag_value: str | None
    allocate_regional_ec2: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            region=os.environ.get("QA_COST_REGION", "us-east-2"),
            instance_id=os.environ.get("QA_COST_INSTANCE_ID") or None,
            tag_key=os.environ.get("QA_COST_TAG_KEY") or None,
            tag_value=os.environ.get("QA_COST_TAG_VALUE") or None,
            allocate_regional_ec2=os.environ.get("QA_COST_ALLOCATE_REGIONAL_EC2", "").strip().lower()
            in ("1", "true", "yes"),
        )

    @property
    def tagged(self) -> bool:
        return bool(self.tag_key and self.tag_value)


# ── classification ─────────────────────────────────────────────────────────


_REGION_PREFIX = re.compile(r"^[A-Z]{2,4}\d{0,2}-(?=[A-Za-z])")


def categorize(service: str, usage_type: str) -> tuple[str, str]:
    """(category, billing type) for one Cost Explorer SERVICE/USAGE_TYPE row.
    Usage types carry a region prefix ("USE2-BoxUsage:…"), except us-east-1."""
    u = _REGION_PREFIX.sub("", usage_type)
    if "BoxUsage" in u or "SpotUsage" in u or "DedicatedUsage" in u or "HostUsage" in u:
        return "EC2 compute", "On-Demand, per hour"
    if "EBS:Snapshot" in u:
        return "EBS snapshots", "Storage, per GB-month"
    if u.startswith("EBS:"):
        return "EBS volumes", "Provisioned, per GB-month (+IOPS/throughput above baseline)"
    if "PublicIPv4" in u or "ElasticIP" in u:
        return "Public IPv4", "Per address-hour"
    if "NatGateway" in u:
        return "NAT gateway", "Per hour + per GB"
    if "LoadBalancer" in u or "LCUUsage" in u:
        return "Load balancer", "Per hour + LCU"
    if "DataTransfer" in u or u.endswith("-Out-Bytes") or u.endswith("-In-Bytes") or "Bytes" in u:
        return "Data transfer", "Per GB"
    if service == "AmazonCloudWatch" or u.startswith("CW:") or "CloudWatch" in u:
        return "CloudWatch", "Per metric / alarm / GB ingested"
    if service == "AWS Backup" or "Backup" in u:
        return "Backup", "Per GB-month"
    return "Other EC2-related", "See usage type"


# ── Cost Explorer calls ────────────────────────────────────────────────────


def _amount(group_or_total: dict, metric: str = METRIC) -> Decimal:
    m = (group_or_total.get("Metrics") or group_or_total.get("Total") or {}).get(metric) or {}
    return Decimal(str(m.get("Amount") or "0"))


def _usage(group: dict) -> tuple[Decimal, str]:
    m = (group.get("Metrics") or {}).get("UsageQuantity") or {}
    return Decimal(str(m.get("Amount") or "0")), m.get("Unit") or ""


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


def _and(*exprs: dict | None) -> dict | None:
    parts = [e for e in exprs if e]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"And": parts}


def _period_label(start: str, month_start: date) -> str:
    return "mtd" if start == month_start.isoformat() else "previous"


def collect(ce: Any, cfg: Config, today: date, ec2: Any | None = None) -> dict[str, Any]:
    """Query Cost Explorer and build the dashboard payload. Raises on AWS errors;
    run() turns those into an 'unavailable' payload."""
    month_start = today.replace(day=1)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    end = today + timedelta(days=1)          # End is exclusive; includes today's estimate
    period = {"Start": prev_start.isoformat(), "End": end.isoformat()}
    region_f = {"Dimensions": {"Key": "REGION", "Values": [cfg.region]}}
    family_f = {"Dimensions": {"Key": "SERVICE", "Values": EC2_FAMILY}}
    tag_f = {"Tags": {"Key": cfg.tag_key, "Values": [cfg.tag_value]}} if cfg.tagged else None

    # 1. whole account by service and record type (credits split out)
    acct = _paged(ce, TimePeriod=period, Granularity="MONTHLY", Metrics=[METRIC],
                  GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}])
    # 2. regional EC2-family by service + usage type, with usage quantities
    regional = _paged(ce, TimePeriod=period, Granularity="MONTHLY", Metrics=[METRIC, "UsageQuantity"],
                      Filter=_and(region_f, family_f, NOT_CREDITS),
                      GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
    # 3. tagged QA cost by service + usage type
    tagged = _paged(ce, TimePeriod=period, Granularity="MONTHLY", Metrics=[METRIC, "UsageQuantity"],
                    Filter=_and(tag_f, NOT_CREDITS),
                    GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]) if tag_f else []

    def rows(results: list[dict]) -> dict[str, dict[tuple[str, str], dict]]:
        by: dict[str, dict[tuple[str, str], dict]] = {"mtd": {}, "previous": {}}
        for r in results:
            label = _period_label(r["TimePeriod"]["Start"], month_start)
            for g in r.get("Groups") or []:
                svc, ut = (g["Keys"] + [""])[:2]
                qty, unit = _usage(g)
                by[label][(svc, ut)] = {"cost": _amount(g), "qty": qty, "unit": unit, "estimated": bool(r.get("Estimated"))}
        return by

    reg, tag = rows(regional), rows(tagged)
    acct_by: dict[str, dict[str, Decimal]] = {"mtd": {}, "previous": {}}
    credits = {"mtd": Decimal(0), "previous": Decimal(0)}
    estimated = False
    for r in acct:
        label = _period_label(r["TimePeriod"]["Start"], month_start)
        estimated |= label == "mtd" and bool(r.get("Estimated"))
        for g in r.get("Groups") or []:
            svc, rtype = (g["Keys"] + [""])[:2]
            if rtype in CREDIT_TYPES:
                credits[label] += _amount(g)
            else:
                acct_by[label][svc] = acct_by[label].get(svc, Decimal(0)) + _amount(g)

    def split(label: str) -> tuple[list[dict], Decimal, Decimal]:
        """Service rows for one period: direct (tag) first, then the regional
        remainder as allocated. A cost is in exactly one bucket."""
        items, direct, alloc = [], Decimal(0), Decimal(0)
        keys = set(tag[label]) | (set(reg[label]) if cfg.allocate_regional_ec2 else set())
        for key in sorted(keys):
            svc, ut = key
            t = tag[label].get(key)
            r = reg[label].get(key) if cfg.allocate_regional_ec2 else None
            t_cost = t["cost"] if t else Decimal(0)
            a_cost = max(Decimal(0), (r["cost"] if r else Decimal(0)) - t_cost)
            if t_cost == 0 and a_cost == 0:
                continue
            cat, billing = categorize(svc, ut)
            src = t or r
            items.append({
                "category": cat, "service": svc, "usage_type": ut,
                "resource": _resource_hint(cat, cfg),
                "usage": f"{src['qty'].normalize():f} {src['unit']}".strip() if src and src["qty"] else "",
                "direct_usd": str(t_cost.quantize(Decimal("0.01"))),
                "allocated_usd": str(a_cost.quantize(Decimal("0.01"))),
                "cost_usd": str((t_cost + a_cost).quantize(Decimal("0.01"))),
                "billing_type": billing,
            })
            direct += t_cost
            alloc += a_cost
        return items, direct, alloc

    mtd_items, mtd_direct, mtd_alloc = split("mtd")
    prev_items, prev_direct, prev_alloc = split("previous")
    qa_mtd = mtd_direct + mtd_alloc
    qa_prev = prev_direct + prev_alloc
    acct_mtd = sum(acct_by["mtd"].values(), Decimal(0))
    acct_prev = sum(acct_by["previous"].values(), Decimal(0))
    prod_mtd = sum((v for k, v in acct_by["mtd"].items() if k in PRODUCTION_SERVICES), Decimal(0))

    # 4. daily trend, same scope as the QA total
    qa_filter = _qa_filter(cfg, region_f, family_f, tag_f)
    daily: list[dict] = []
    if qa_filter:
        for r in _paged(ce, TimePeriod={"Start": month_start.isoformat(), "End": end.isoformat()},
                        Granularity="DAILY", Metrics=[METRIC], Filter=qa_filter):
            daily.append({"date": r["TimePeriod"]["Start"], "cost_usd": str(_amount(r).quantize(Decimal("0.01"))),
                          "estimated": bool(r.get("Estimated"))})
    complete = [d for d in daily if date.fromisoformat(d["date"]) < today]
    avg_daily = (sum(Decimal(d["cost_usd"]) for d in complete) / len(complete)) if complete else None

    # 5. projection: AWS forecast for the rest of the month, else a linear estimate
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_end = month_start.replace(day=days_in_month) + timedelta(days=1)
    projected, projection_method = None, None
    if qa_filter:
        try:
            if end < month_end:
                fc = ce.get_cost_forecast(TimePeriod={"Start": end.isoformat(), "End": month_end.isoformat()},
                                          Metric="UNBLENDED_COST", Granularity="MONTHLY", Filter=qa_filter)
                projected = qa_mtd + Decimal(str(fc["Total"]["Amount"]))
            else:
                projected = qa_mtd
            projection_method = "aws_forecast"
        except Exception as exc:  # noqa: BLE001 — new accounts / early month have no forecast
            log.info("forecast unavailable (%s), using linear", _error_code(exc))
            if avg_daily is not None:
                projected = qa_mtd + avg_daily * (days_in_month - len(complete) - 1)
                projection_method = "linear_from_daily_average"

    last_day = max((d["date"] for d in daily if Decimal(d["cost_usd"]) > 0), default=None)
    attributed = cfg.tagged or cfg.allocate_regional_ec2
    q = Decimal("0.01")
    qa_by_service: dict[str, Decimal] = {}
    for it in mtd_items:
        qa_by_service[it["service"]] = qa_by_service.get(it["service"], Decimal(0)) + Decimal(it["cost_usd"])
    unattributed_rows = []
    for svc, v in sorted(acct_by["mtd"].items(), key=lambda kv: -kv[1]):
        rest = v - qa_by_service.get(svc, Decimal(0))
        if rest > Decimal("0.004"):
            unattributed_rows.append({"service": svc, "cost_usd": str(rest.quantize(q)),
                                      "production": svc in PRODUCTION_SERVICES})

    def money(v: Decimal | None) -> str | None:
        return None if v is None else str(v.quantize(q))

    return {
        "status": "ok" if attributed else "no_attribution",
        "currency": "USD",
        "metric": METRIC,
        "basis": "before credits and refunds",
        "region": cfg.region,
        "instance_id": cfg.instance_id,
        "attribution": {
            "direct_source": f"tag {cfg.tag_key}={cfg.tag_value}" if cfg.tagged else None,
            "allocated_source": f"EC2-family services in {cfg.region}" if cfg.allocate_regional_ec2 else None,
            "allocation_verified": _verify_single_instance(ec2, cfg) if cfg.allocate_regional_ec2 else None,
        },
        "month_to_date": {
            "qa_total_usd": money(qa_mtd) if attributed else None,
            "direct_usd": money(mtd_direct) if cfg.tagged else None,
            "allocated_usd": money(mtd_alloc) if cfg.allocate_regional_ec2 else None,
            "unattributed_usd": money(acct_mtd - qa_mtd),
            "production_lightsail_excluded_usd": money(prod_mtd),
            "account_total_usd": money(acct_mtd),
            "account_credits_usd": money(credits["mtd"]),
            "account_net_usd": money(acct_mtd + credits["mtd"]),
            "estimated": estimated,
        },
        "previous_month": {
            "month": prev_start.strftime("%Y-%m"),
            "qa_total_usd": money(qa_prev) if attributed else None,
            "direct_usd": money(prev_direct) if cfg.tagged else None,
            "allocated_usd": money(prev_alloc) if cfg.allocate_regional_ec2 else None,
            "account_total_usd": money(acct_prev),
            "account_credits_usd": money(credits["previous"]),
        },
        "projected_month_usd": money(projected),
        "projection_method": projection_method,
        "average_daily_usd": money(avg_daily),
        "services": mtd_items,
        "previous_services": prev_items,
        "daily": daily,
        "data_through": last_day,
        "unattributed_by_service": unattributed_rows,
    }


def _qa_filter(cfg: Config, region_f: dict, family_f: dict, tag_f: dict | None) -> dict | None:
    """Scope of the QA total: tagged OR (regional EC2-family), before credits —
    the union of the two buckets, so daily/forecast sum to direct + allocated."""
    regional = _and(region_f, family_f) if cfg.allocate_regional_ec2 else None
    if tag_f and regional:
        return {"And": [{"Or": [tag_f, regional]}, NOT_CREDITS]}
    scope = tag_f or regional
    return _and(scope, NOT_CREDITS) if scope else None


def _resource_hint(category: str, cfg: Config) -> str:
    if category == "EC2 compute" and cfg.instance_id:
        return cfg.instance_id
    return ""


def _verify_single_instance(ec2: Any | None, cfg: Config) -> dict[str, Any]:
    """Allocation is only sound if QA is the region's only EC2 instance. Needs
    ec2:DescribeInstances; without it the allocation stays marked unverified."""
    if ec2 is None:
        return {"verified": False, "reason": "no EC2 client"}
    try:
        res = ec2.describe_instances(Filters=[{"Name": "instance-state-name",
                                               "Values": ["pending", "running", "stopping", "stopped"]}])
        ids = [i["InstanceId"] for r in res.get("Reservations", []) for i in r.get("Instances", [])]
    except Exception as exc:  # noqa: BLE001
        return {"verified": False, "reason": f"cannot list instances ({_error_code(exc)})"}
    ok = ids == [cfg.instance_id] if cfg.instance_id else len(ids) == 1
    return {"verified": ok, "instances_in_region": len(ids),
            "reason": None if ok else "other EC2 instances in the region share these charges"}


# ── errors + output ────────────────────────────────────────────────────────


def _error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return (resp.get("Error") or {}).get("Code") or type(exc).__name__
    return type(exc).__name__


_DENIED = f"The QA instance role needs {', '.join(REQUIRED_ACTIONS)} (read-only)."
_HINTS = {
    "AccessDeniedException": _DENIED,
    "AccessDenied": _DENIED,
    "UnauthorizedOperation": _DENIED,
    "DataUnavailableException": "Cost Explorer has no data yet — it can take up to 24h after it is first enabled.",
    "OptInRequired": "Cost Explorer isn't enabled for this account (Billing console → Cost Explorer).",
    "NoCredentialsError": "The collector on the QA host can't reach the EC2 instance role.",
    "boto3_missing": "Install python3-boto3 on the QA host.",
}


def unavailable(reason: str, code: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "error_code": code,
            "action_needed": _HINTS.get(code, "AWS returned an error; see `journalctl -u kopyya-aws-costs`."),
            "required_permissions": REQUIRED_ACTIONS}


def run(cfg: Config, now: datetime, clients=None) -> dict[str, Any]:
    try:
        ce, ec2 = (clients or _default_clients)(cfg)
    except ImportError:
        payload = unavailable("boto3 not installed on the host", "boto3_missing")
    else:
        try:
            payload = collect(ce, cfg, now.date(), ec2=ec2)
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            log.warning("Cost Explorer query failed: %s", code)
            payload = unavailable(f"Cost Explorer query failed ({code})", code)
    payload["fetched_at"] = now.isoformat()
    payload["interval_seconds"] = INTERVAL_S
    return payload


def _default_clients(cfg: Config) -> tuple[Any, Any]:
    import boto3  # noqa: PLC0415

    # Cost Explorer is served from us-east-1 regardless of where resources run.
    return boto3.client("ce", region_name="us-east-1"), boto3.client("ec2", region_name=cfg.region)


def write(payload: dict[str, Any], out: str = OUT) -> None:
    d = os.path.dirname(out)
    os.makedirs(d, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".aws-costs.")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload = run(Config.from_env(), datetime.now(timezone.utc))
    write(payload)
    log.info("status=%s qa_mtd=%s", payload["status"], (payload.get("month_to_date") or {}).get("qa_total_usd"))
    return 0 if payload["status"] != "unavailable" else 1


if __name__ == "__main__":
    sys.exit(main())
