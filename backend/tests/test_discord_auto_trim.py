"""Auto Trim: fire a ladder rung when its profit gate is reached.

    1st Trim, Min Profit 20%, Auto Trim ON
        position reaches +20%  ->  sell half, stop the rest below entry

Nothing here decides what a trim DOES — it works out WHICH rung is next and
whether its gate has been reached, then submits the exit through the ordinary
alert pipeline. The tests below are about the trigger; the trim itself is
covered by the ladder's own tests, which is the point of not duplicating it.
"""
import inspect
import os
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_auto_trim as at
from app.models.order import OptionRight

EXP = date(2026, 9, 25)


def _ts(on=True, g1="20", g2="0", g3="0"):
    return SimpleNamespace(
        discord_auto_trim=on,
        discord_trim_profit_gate_pct=Decimal(g1),
        discord_trim2_profit_gate_pct=Decimal(g2),
        discord_trim3_profit_gate_pct=Decimal(g3),
    )


def _guard(sell_count=0, entry="1.00"):
    return SimpleNamespace(
        symbol="SPY", option_strike=Decimal("771"), option_right=OptionRight.CALL,
        option_expiry=EXP, sell_count=sell_count,
        entry_price=Decimal(entry) if entry is not None else None,
    )


# ── the trigger ─────────────────────────────────────────────────────────────

def test_the_gate_fires_the_rung():
    """+20% on a 20% gate. The example from the spec."""
    assert at.due_rung(_ts(), _guard(), Decimal("1.20")) == 1


def test_the_gate_is_inclusive():
    """"up 20%" trims AT a 20% threshold, exactly as the alert path's gate
    does — the two must not disagree about the same number."""
    assert at.due_rung(_ts(g1="20"), _guard(), Decimal("1.20")) == 1


def test_below_the_gate_nothing_fires():
    assert at.due_rung(_ts(), _guard(), Decimal("1.19")) is None


def test_a_loss_never_fires():
    assert at.due_rung(_ts(), _guard(), Decimal("0.50")) is None


def test_it_is_off_unless_the_trader_turns_it_on():
    """Every existing trader keeps alert-driven trimming untouched."""
    assert at.due_rung(_ts(on=False), _guard(), Decimal("2.00")) is None


# ── which rung ──────────────────────────────────────────────────────────────

def test_it_advances_with_the_ladder():
    """Rung 2 is next once rung 1 has fired, and it reads ITS own gate."""
    ts = _ts(g1="20", g2="50")
    assert at.due_rung(ts, _guard(sell_count=1), Decimal("1.40")) is None   # under 50%
    assert at.due_rung(ts, _guard(sell_count=1), Decimal("1.50")) == 2


def test_each_rung_is_independent():
    ts = _ts(g1="20", g2="50", g3="100")
    assert at.due_rung(ts, _guard(sell_count=2), Decimal("1.99")) is None
    assert at.due_rung(ts, _guard(sell_count=2), Decimal("2.00")) == 3


def test_a_rung_does_not_fire_twice():
    """The guard's sell_count is the rung counter and plan_exit advances it, so
    a fired rung cannot fire again while the price stays above its gate."""
    ts = _ts(g1="20")
    assert at.due_rung(ts, _guard(sell_count=0), Decimal("2.00")) == 1
    # Same price, rung already taken, and rung 2 has no threshold.
    assert at.due_rung(ts, _guard(sell_count=1), Decimal("2.00")) is None


def test_nothing_fires_past_the_third_rung():
    """The ladder has three steps; the third exits what remains."""
    assert at.due_rung(_ts(g3="10"), _guard(sell_count=3), Decimal("5.00")) is None


# ── a gate of 0 is never automatic ──────────────────────────────────────────

def test_a_zero_gate_is_never_auto_fired():
    """Zero means "no minimum" — right for an ALERT-driven rung, meaningless
    without one, because "reached 0% profit" is true the instant a position is
    up a cent. Auto-firing those would walk rungs 2 and 3 immediately after
    rung 1 and flatten the position on the same tick."""
    assert at.due_rung(_ts(g1="0"), _guard(), Decimal("1.50")) is None


def test_the_default_settings_only_automate_the_first_trim():
    """Defaults are 20 / 0 / 0. Turning Auto Trim on must not silently flatten
    a position the moment it goes green."""
    ts = _ts(g1="20", g2="0", g3="0")
    assert at.due_rung(ts, _guard(sell_count=0), Decimal("1.50")) == 1
    assert at.due_rung(ts, _guard(sell_count=1), Decimal("1.50")) is None
    assert at.due_rung(ts, _guard(sell_count=2), Decimal("1.50")) is None


# ── measuring the profit ────────────────────────────────────────────────────

def test_profit_is_measured_from_the_ladder_reference():
    """The same number every gate and stop keys off — so averaging down, which
    re-weights that reference, moves the auto-trim level with the real cost."""
    assert at.gain_pct(Decimal("1.00"), Decimal("1.20")) == Decimal(20)
    assert at.gain_pct(Decimal("0.385"), Decimal("0.4620")) == Decimal(20)


@pytest.mark.parametrize("entry,mark", [
    (None, Decimal("1.20")), (Decimal("1.00"), None),
    (Decimal(0), Decimal("1.20")), (Decimal("1.00"), Decimal(0)),
])
def test_an_unusable_price_never_fires(entry, mark):
    """No reference or no mark means the profit is unknown — and an unknown
    profit must not be read as "gate reached"."""
    assert at.gain_pct(entry, mark) is None
    assert at.due_rung(_ts(), _guard(entry=entry), mark) is None


# ── it reuses the one trim path ─────────────────────────────────────────────

def test_it_fires_through_the_normal_alert_pipeline():
    """A second trim path would be a second thing to keep correct, and it
    would be the one nobody tests."""
    src = inspect.getsource(at.tick)
    assert "submit_self_alert_text(db, user, text, approve=True)" in src


def test_the_synthetic_alert_is_a_real_exit_the_parser_reads():
    """Written as TEXT on purpose — read by the same parser as the author's
    own trims, so the two cannot drift apart."""
    from datetime import datetime, timezone

    from app.services.discord_parsers import parse_message
    from app.services.discord_parsers.base import ParsedMessage, ParseStatus, SignalAction

    text = at._exit_alert_text(_guard())
    r = parse_message(ParsedMessage(content=text, posted_at=datetime.now(timezone.utc)))
    assert r.status is ParseStatus.PARSED
    s = r.signals[0]
    assert s.action is SignalAction.SELL
    assert s.symbol == "SPY"
    assert s.strike == Decimal("771")
    assert s.option_type.value == "CALL"
    assert s.expiration == EXP


def test_an_auto_trim_does_not_wait_for_a_second_approval():
    """The trader turning Auto Trim on IS the approval. Sitting in the manual
    queue would miss the move it was watching for."""
    src = inspect.getsource(at.tick)
    assert "approve=True" in src
