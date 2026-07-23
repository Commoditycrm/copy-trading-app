"""Unit tests for the daily profit-target trigger + once-per-day guard.

Covers pnl_poller._enforce_profit_target and _clear_profit_target_guard_if_new_day
without touching a broker or DB: the broker close, audit, and notification calls
are monkeypatched. We assert the TRIGGER math (equity-vs-baseline), the
once-per-day guard, that copy_enabled is NEVER touched, and that cascade_fanout
threads through for the trader case.

Standalone: ``.venv/bin/python tests/test_profit_target.py`` or under pytest.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.pnl_poller as poller
from app.services import audit as audit_mod
from app.services import notifications as notif_mod
import app.api.positions as positions_mod


class _Settings:
    """Stand-in for a SubscriberSettings row."""
    def __init__(self, pct, hit_at=None, copy_enabled=True):
        self.user_id = uuid.uuid4()
        self.following_trader_id = None
        self.daily_profit_target_pct = Decimal(str(pct)) if pct is not None else None
        self.profit_target_hit_at = hit_at
        self.copy_enabled = copy_enabled


class _Broker:
    value = "snaptrade"


class _Acct:
    def __init__(self):
        self.id = uuid.uuid4()
        self.broker = _Broker()


def _patch(monkeypatch_calls):
    """Replace side-effecting deps with capturing fakes. Returns a dict that
    records the close-call kwargs so tests can assert on them."""
    captured = {"close_calls": []}

    def fake_close(sub_id, acct_id, trader_id, ip, filt, option_marketable_limit=False, cascade_fanout=False):
        captured["close_calls"].append({
            "sub_id": sub_id, "acct_id": acct_id,
            "option_marketable_limit": option_marketable_limit,
            "cascade_fanout": cascade_fanout,
        })
        return {"closed": [{"symbol": "AAPL"}], "failed": []}

    positions_mod._close_account_positions_sync = fake_close
    audit_mod.record = lambda *a, **k: None
    notif_mod.create_notification = lambda *a, **k: None
    monkeypatch_calls.append(captured)
    return captured


def test_fires_when_value_reaches_target():
    """base 1000, 20% → target profit 200. equity 1200 (todays_pl 200) → fire."""
    cap = _patch([])
    s = _Settings(pct=20)
    fired = poller._enforce_profit_target(
        db=None, s=s, acct=_Acct(),
        equity=Decimal("1200"), todays_pl=Decimal("200"),
        beginning_day_balance=Decimal("1000"),
        now_utc=datetime.now(timezone.utc), cascade_fanout=False, pending_events=[],
    )
    assert fired is True
    assert s.profit_target_hit_at is not None          # guard stamped
    assert s.copy_enabled is True                       # copy NEVER disabled
    assert len(cap["close_calls"]) == 1
    assert cap["close_calls"][0]["option_marketable_limit"] is True  # option-safe close


def test_does_not_fire_below_target():
    """todays_pl 199 < 200 target → no liquidation."""
    cap = _patch([])
    s = _Settings(pct=20)
    fired = poller._enforce_profit_target(
        db=None, s=s, acct=_Acct(),
        equity=Decimal("1199"), todays_pl=Decimal("199"),
        beginning_day_balance=Decimal("1000"),
        now_utc=datetime.now(timezone.utc), cascade_fanout=False, pending_events=[],
    )
    assert fired is False
    assert s.profit_target_hit_at is None
    assert cap["close_calls"] == []


def test_once_per_day_guard_blocks_reliquidation():
    """Already hit today → do not liquidate again even though equity is above."""
    cap = _patch([])
    s = _Settings(pct=20, hit_at=datetime.now(timezone.utc))
    fired = poller._enforce_profit_target(
        db=None, s=s, acct=_Acct(),
        equity=Decimal("1300"), todays_pl=Decimal("300"),
        beginning_day_balance=Decimal("1000"),
        now_utc=datetime.now(timezone.utc), cascade_fanout=False, pending_events=[],
    )
    assert fired is False
    assert cap["close_calls"] == []


def test_guard_clears_on_new_utc_day():
    """A guard stamped yesterday is cleared so the target re-arms today."""
    s = _Settings(pct=20, hit_at=datetime.now(timezone.utc) - timedelta(days=1))
    poller._clear_profit_target_guard_if_new_day(s, datetime.now(timezone.utc))
    assert s.profit_target_hit_at is None
    # A same-day guard is left intact.
    now = datetime.now(timezone.utc)
    s2 = _Settings(pct=20, hit_at=now)
    poller._clear_profit_target_guard_if_new_day(s2, now)
    assert s2.profit_target_hit_at is not None


def test_disabled_when_pct_none_or_no_baseline():
    cap = _patch([])
    # pct not set
    assert poller._enforce_profit_target(
        db=None, s=_Settings(pct=None), acct=_Acct(),
        equity=Decimal("2000"), todays_pl=Decimal("1000"),
        beginning_day_balance=Decimal("1000"),
        now_utc=datetime.now(timezone.utc), cascade_fanout=False, pending_events=[],
    ) is False
    # no beginning_day_balance (SnapTrade broker without day-start)
    assert poller._enforce_profit_target(
        db=None, s=_Settings(pct=20), acct=_Acct(),
        equity=Decimal("2000"), todays_pl=Decimal("1000"),
        beginning_day_balance=None,
        now_utc=datetime.now(timezone.utc), cascade_fanout=False, pending_events=[],
    ) is False
    assert cap["close_calls"] == []


def test_trader_cascade_flag_threads_through():
    """Trader liquidation must cascade to subscribers (cascade_fanout=True)."""
    cap = _patch([])
    s = _Settings(pct=10)
    poller._enforce_profit_target(
        db=None, s=s, acct=_Acct(),
        equity=Decimal("1100"), todays_pl=Decimal("100"),
        beginning_day_balance=Decimal("1000"),
        now_utc=datetime.now(timezone.utc), cascade_fanout=True, pending_events=[],
    )
    assert cap["close_calls"][0]["cascade_fanout"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll profit-target tests passed.")
