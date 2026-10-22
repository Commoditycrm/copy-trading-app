"""Concurrent Calendar / Positions page loads against a running backend, to
check the DB pool holds up (QueuePool timeouts show up as 500s).

Places and cancels nothing. GET /api/calendar/pnl runs the same fills sync the
Calendar page does on load; --with-sync adds POST /api/trades/sync-fills
(what the page calls on mount) — both only record fills the broker already
has.

    KOPYYA_TOKEN=<bearer jwt> python -m scripts.load_test_pages \
        --base-url https://qa.example.com --rounds 5 --concurrency 40

Watch Postgres meanwhile:

    SELECT state, count(*) FROM pg_stat_activity
    WHERE datname = current_database() GROUP BY state;
"""
import argparse
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx


def _endpoints(with_sync: bool) -> list[tuple[str, str, dict]]:
    today = datetime.now(ZoneInfo("America/New_York")).date()
    first = date(today.year, today.month, 1)
    eps = [
        ("GET", "/api/calendar/pnl",
         {"from": first.isoformat(), "to": today.isoformat(), "tz": "America/New_York"}),
        ("GET", "/api/positions", {"detail": "1"}),
        ("GET", "/api/positions/day-pnl", {}),
    ]
    if with_sync:
        eps.append(("POST", "/api/trades/sync-fills", {}))
    return eps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--with-sync", action="store_true")
    args = ap.parse_args()
    token = os.environ.get("KOPYYA_TOKEN")
    if not token:
        print("set KOPYYA_TOKEN to a bearer token for the user to load as", file=sys.stderr)
        return 2

    eps = _endpoints(args.with_sync)
    client = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.timeout,
                          headers={"Authorization": f"Bearer {token}"},
                          limits=httpx.Limits(max_connections=args.concurrency))
    statuses: dict[str, Counter] = defaultdict(Counter)
    latencies: dict[str, list[float]] = defaultdict(list)
    errors: list[str] = []

    def hit(i: int) -> None:
        method, path, params = eps[i % len(eps)]
        t0 = time.monotonic()
        try:
            r = client.request(method, path, params=params)
            code = str(r.status_code)
            if r.status_code >= 500:
                errors.append(f"{path} {r.status_code} {r.text[:200]}")
        except httpx.HTTPError as exc:
            code = type(exc).__name__
            errors.append(f"{path} {code}")
        latencies[path].append(time.monotonic() - t0)
        statuses[path][code] += 1

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for rnd in range(args.rounds):
            t0 = time.monotonic()
            list(pool.map(hit, range(args.concurrency)))
            print(f"round {rnd + 1}/{args.rounds}: {time.monotonic() - t0:.1f}s")

    print()
    for path, lats in latencies.items():
        lats.sort()
        p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
        print(f"{path:28} n={len(lats):4} {dict(statuses[path])} "
              f"p50={statistics.median(lats):.2f}s p95={p95:.2f}s max={lats[-1]:.2f}s")
    for e in errors[:10]:
        print("  !", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
