"""Fake broker adapter for load-testing the fanout pipeline.

WHY THIS EXISTS
---------------
The Performance page lets us measure platform-side latency (pick lag,
eligibility lag, publish lag) per trade. Measuring those with the real
Alpaca adapter is unreliable because:

  1. Alpaca paper API rate-limits at ~200 req/min per account. A
     100-subscriber fanout fired in parallel will hit the limit and the
     resulting latencies measure throttling, not our platform.
  2. Real broker round-trip variance (200ms p50 → 15s outliers) drowns out
     the platform-side metric we care about.
  3. Creating 100 separate paper accounts is impractical and pointless.

The fake adapter replaces ``adapter.place_order()`` with a sleep of
configurable duration and returns a synthetic ``BrokerOrderResult``. No
network, no rate limit, no Alpaca account needed. Subscribers seeded with
``broker = "fake"`` route here instead of Alpaca.

NEVER USE IN PRODUCTION FANOUT
------------------------------
A FakeBrokerAdapter returns "submitted" without sending the order
anywhere. If a real subscriber's account is somehow flagged ``broker =
"fake"``, the trader's order will look successful to them but their
broker account will see nothing. The seeded test users we create
explicitly have ``email LIKE 'fake-load-test-%@example.invalid'`` — keep
that pattern reserved.

PROFILES
--------
Selected via ``MOCK_BROKER_PROFILE`` env var (default "flat"):

  flat        Every call sleeps MOCK_BROKER_LATENCY_MS (default 300ms).
              Best for isolating platform-side performance — no broker
              variance.

  realistic   ~Alpaca-like distribution:
                p50  ≈ 300ms
                p99  ≈ 1.5s
                + MOCK_BROKER_SLOW_PROBABILITY (default 1%) of calls
                  sleep MOCK_BROKER_SLOW_LATENCY_MS (default 12000ms) to
                  simulate a rate-limited or stuck account — the kind of
                  outlier you see on the real Performance page.

All knobs are runtime-overridable so a single test rig can sweep multiple
configurations without restart.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import OrderStatus

log = logging.getLogger(__name__)


# ── Profiles ────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("fake broker: %s=%r is not an int, using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("fake broker: %s=%r is not a float, using default %.3f", name, raw, default)
        return default


def _profile() -> str:
    p = (os.environ.get("MOCK_BROKER_PROFILE") or "flat").strip().lower()
    if p not in ("flat", "realistic"):
        log.warning("fake broker: MOCK_BROKER_PROFILE=%r unknown, falling back to 'flat'", p)
        return "flat"
    return p


def _sleep_duration_s() -> float:
    """Pick how long this individual call should sleep, per the active profile.

    Re-reads env on every call so an operator can change settings without
    restarting the worker — handy when sweeping a test."""
    profile = _profile()

    if profile == "flat":
        base_ms = _env_int("MOCK_BROKER_LATENCY_MS", 300)
        jitter_ms = _env_int("MOCK_BROKER_JITTER_MS", 0)
        if jitter_ms > 0:
            base_ms += random.randint(-jitter_ms, jitter_ms)
        return max(0, base_ms) / 1000.0

    # realistic profile: log-normal-ish distribution + slow-outlier tail.
    slow_prob = _env_float("MOCK_BROKER_SLOW_PROBABILITY", 0.01)
    slow_ms = _env_int("MOCK_BROKER_SLOW_LATENCY_MS", 12000)
    if random.random() < slow_prob:
        # Slow-outlier path: simulates a rate-limited account or stuck order.
        return slow_ms / 1000.0

    # Normal path: log-normal-ish around ~300ms with a tail out to ~1.5s.
    # Shape comes from random.lognormvariate; coefficients tuned by eye to
    # match the spread we see on the real Performance page.
    mu = -1.2     # ln(median in seconds) — exp(-1.2) ≈ 0.30
    sigma = 0.45  # spread
    s = random.lognormvariate(mu, sigma)
    return min(s, 3.0)  # clip at 3s — anything slower goes through the slow-prob path


# ── Adapter ─────────────────────────────────────────────────────────────────


class FakeBrokerAdapter(BrokerAdapter):
    """Drop-in stand-in for AlpacaAdapter. Sleeps + returns synthetic results.

    Credentials are accepted but ignored — the seed script stores an empty
    encrypted dict so the existing decrypt path in copy_engine doesn't have
    to branch on broker type.
    """

    name = "fake"

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)

    def verify_connection(self) -> ConnectionInfo:
        # Cheap, instant — matches the shape of AlpacaAdapter.verify_connection().
        # supports_fractional=True so the copy engine doesn't floor mirror
        # quantities when one of the test users has a small multiplier.
        return ConnectionInfo(
            broker_account_id=f"fake-{uuid.uuid4().hex[:8]}",
            supports_fractional=True,
            extra={"profile": _profile(), "mocked": True},
        )

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        # Sleep first — this is the entire point. Done on the calling thread
        # because copy_engine wraps the call in asyncio.to_thread, so the
        # event loop stays free during the sleep.
        time.sleep(_sleep_duration_s())

        submitted_at = datetime.now(timezone.utc)
        # Mirror the field shape Alpaca returns. We say "submitted" rather
        # than "filled" so the fake doesn't synthesise fills that would
        # confuse downstream P&L code — same as a real broker's immediate
        # accept response.
        return BrokerOrderResult(
            broker_order_id=f"fake-{uuid.uuid4().hex}",
            status=OrderStatus.SUBMITTED,
            submitted_at=submitted_at,
            filled_quantity=Decimal(0),
            filled_avg_price=None,
            reject_reason=None,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        # If a poller asks for the fake order back, claim it filled. Keeps
        # the Order History page from looking "stuck pending" forever in
        # test runs. Uses now() rather than tracking real timestamps; the
        # fake adapter has no persistent state.
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal(1),
            filled_avg_price=Decimal("100.00"),
            reject_reason=None,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        # Tiny sleep so cancel-fanout latency in the metrics isn't an
        # unrealistic zero.
        time.sleep(0.05)

    def get_positions(self) -> list[BrokerPosition]:
        # Test users don't have real positions; the positions page will
        # show empty for fake accounts, which is what we want.
        return []
