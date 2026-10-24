"""A late ENTRY alert is not placed automatically.

A restarted or reconnecting Discord listener replays everything posted while
it was away. In auto mode those used to be placed on arrival — a QA restart
bought the whole backlog at the prices of the moment. Now an entry that
arrives more than the allowed age after it was posted waits for approval.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.discord_message import SignalDecision
from app.services import discord_freshness as fresh

NOW = datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc)
USER = uuid.uuid4()


def _snowflake(when: datetime) -> str:
    return str((int(when.timestamp() * 1000) - 1_420_070_400_000) << 22)


def _msg(age_s, action="BUY", decision=SignalDecision.APPROVED, posted=True, author="112233445566"):
    when = NOW - timedelta(seconds=age_s)
    return SimpleNamespace(
        decision=decision, decision_mode="auto", decided_at=NOW, status_reason=None,
        parsed_signal={"action": action, "symbol": "SPY"}, user_id=USER, author_id=author,
        posted_at=when if posted else None, discord_message_id=_snowflake(when),
    )


def test_a_backlog_entry_waits_for_approval():
    m = _msg(age_s=7 * 60)
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is True
    assert m.decision is SignalDecision.PENDING and m.decision_mode == "manual" and m.decided_at is None
    assert "7 min" in m.status_reason and "Approve it" in m.status_reason


def test_a_fresh_entry_goes_through():
    m = _msg(age_s=20)
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is False
    assert m.decision is SignalDecision.APPROVED and m.status_reason is None


def test_right_at_the_limit_still_goes_through():
    assert fresh.hold_if_stale(_msg(age_s=120), max_age_s=120, now=NOW) is False


def test_a_late_exit_is_not_held():
    """A trim or close only reduces a position the author has already left."""
    m = _msg(age_s=15 * 60, action="SELL")
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is False
    assert m.decision is SignalDecision.APPROVED


def test_an_alert_the_trader_typed_is_never_held():
    m = _msg(age_s=15 * 60, author=str(USER))
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is False


@pytest.mark.parametrize("decision", [SignalDecision.PENDING, SignalDecision.REJECTED, None])
def test_only_auto_approved_alerts_are_touched(decision):
    m = _msg(age_s=15 * 60, decision=decision)
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is False
    assert m.decision is decision and m.status_reason is None


def test_without_discords_timestamp_the_snowflake_tells_the_time():
    m = _msg(age_s=10 * 60, posted=False)
    assert abs((fresh.posted_at(m) - (NOW - timedelta(minutes=10))).total_seconds()) < 1
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is True


def test_no_time_at_all_is_not_held():
    m = _msg(age_s=10 * 60, posted=False)
    m.discord_message_id = "not-a-snowflake"
    assert fresh.hold_if_stale(m, max_age_s=120, now=NOW) is False


def test_the_limit_comes_from_settings_by_default(monkeypatch):
    import app.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: SimpleNamespace(discord_max_alert_age_s=600))
    assert fresh.hold_if_stale(_msg(age_s=5 * 60), now=NOW) is False      # inside 10 min
    assert fresh.hold_if_stale(_msg(age_s=11 * 60), now=NOW) is True
