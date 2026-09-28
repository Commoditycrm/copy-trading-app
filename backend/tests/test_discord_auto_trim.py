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
import uuid
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


# ── the strike must survive the round trip ──────────────────────────────────
#
# Live 2026-09-28: Decimal("230").normalize() renders as 2.3E+2, so auto-trim
# wrote "✂️ $NVDA 2.3E+2C 09/28" and the message was stored as "not a trade
# alert". Every strike ending in a zero was silently un-trimmable; NVDA only
# looked healthy because real Discord alerts were driving it.

@pytest.mark.parametrize("strike", ["230", "741", "342.5", "769", "3.5",
                                    "6000", "342.50", "20", "0.5"])
def test_every_strike_parses_back_to_itself(strike):
    from datetime import datetime, timezone

    from app.services.discord_parsers import parse_message
    from app.services.discord_parsers.base import ParsedMessage, ParseStatus

    guard = _guard()
    guard.option_strike = Decimal(strike)
    guard.option_expiry = EXP
    text = at._exit_alert_text(guard)
    assert "E+" not in text and "e+" not in text, text

    r = parse_message(ParsedMessage(content=text, posted_at=datetime.now(timezone.utc)))
    assert r.status is ParseStatus.PARSED, text
    assert r.signals[0].strike == Decimal(strike), text


@pytest.mark.parametrize("raw,want", [
    ("230", "230"), ("342.50", "342.5"), ("3.5", "3.5"), ("6000", "6000"),
])
def test_the_strike_is_written_the_way_a_human_writes_it(raw, want):
    assert at._strike_text(Decimal(raw)) == want


# ── it must measure against the FILL, not the limit we bid ──────────────────

def test_the_entry_is_synced_before_the_gate_is_measured():
    """QQQ was bid 0.65, repriced to 0.72 and filled at 0.6875. Measuring off
    0.65 read +7.7% and fired; the ladder then re-synced to 0.6875 and answered
    "up 1.82%, under the 5% gate" — a rung spent on a price nobody paid."""
    import inspect

    src = inspect.getsource(at.tick)
    sync_at = src.index("pg.sync_entry_price(db, guard)")
    measure_at = src.index("rung = due_rung(ts, guard, mark)")
    assert sync_at < measure_at, "the entry must be synced BEFORE the gate is read"


def test_the_two_gate_checks_agree_on_the_same_entry():
    """due_rung and plan_exit must read the same reference, or auto-trim fires
    rungs the ladder then refuses."""
    entry, mark = Decimal("0.6875"), Decimal("0.70")
    ts = _ts(g1="5")
    guard = _guard(entry=str(entry))
    assert at.due_rung(ts, guard, mark) is None            # 1.82% < 5%
    # ...and off the stale limit it would wrongly have fired:
    assert at.due_rung(ts, _guard(entry="0.65"), mark) == 1


# ── a rung that did nothing is not spent ────────────────────────────────────

def test_an_unspent_rung_is_returned():
    """plan_exit advances the rung unconditionally, which is right for a HUMAN
    alert — the trader's Nth alert is their Nth trim. Auto-trim has no alert,
    so a rung that turns out not to be due must not be spent, or the next sweep
    measures the rung after it and the ladder walks itself out."""
    import inspect

    src = inspect.getsource(at.tick)
    assert "pg.rollback_exit(guard)" in src
    # Judged on what HAPPENED, not on the note text.
    assert "msg.order_id is not None" in src
    assert "guard.trail_qty != before_trail" in src


def test_a_rung_that_armed_a_trail_is_kept():
    """An expensive contract leaves on a trailing give-back, so the rung places
    no order — rolling that back would arm the same trail every sweep."""
    import inspect

    src = inspect.getsource(at.tick)
    at_check = src.index("did_something = (")
    assert "guard.trail_qty != before_trail" in src[at_check:at_check + 260]


def test_the_alert_path_still_spends_its_rung():
    """The one thing this fix must NOT change. plan_exit advances the rung for
    every caller; only auto-trim hands it back."""
    import inspect

    import app.services.discord_position_guard as g

    src = inspect.getsource(g.plan_exit)
    assert "guard.sell_count = rung" in src
    assert "rollback" not in src.lower()


# ── cadence ─────────────────────────────────────────────────────────────────

def test_the_sweep_is_one_cadence_for_every_broker():
    """Each sweep costs one get_positions per account, and the budget is
    shared: Webull allows 10 per 30 SECONDS across the pnl poller, the order
    listener and this. A broker-aware cadence was tried and dropped — the few
    seconds it saved on Alpaca were not worth two code paths, because the
    detection lag is all this bounds. The stop now goes on within ~1s of the
    fill regardless (pnl_poller.poll_now)."""
    assert at.POLL_INTERVAL_S == 15


def test_positions_are_read_once_per_account_not_per_guard():
    """Reading them per-guard is what blew Webull's limit."""
    import inspect

    src = inspect.getsource(at.tick)
    assert src.count("adapter.get_positions()") == 1
    # ...and the per-guard loop takes the already-fetched list.
    assert "_mark_for(positions, guard)" in src


# ── the stop is placed as soon as the trim fills ────────────────────────────

def test_a_filled_trim_makes_the_account_due_now():
    """The remainder is unprotected until the stop reconciler runs — up to the
    account's whole poll interval (4-13s measured live on Alpaca)."""
    import inspect

    from app.api.discord_sources import _execute_signal

    src = inspect.getsource(_execute_signal)
    assert "poll_now(resolved.broker_account_id)" in src
    # ONLY once filled: reconcile sizes the stop from what the BROKER reports,
    # so poking it mid-settlement would size the stop to the whole position and
    # block the very sell that just went out.
    assert "if is_trim and order.status is OrderStatus.FILLED:" in src


def test_poll_now_only_clears_that_account():
    """It must not reset anyone else's timer — that would multiply position
    reads across every account on the box."""
    import uuid as _u

    from app.services import pnl_poller

    a, b = _u.uuid4(), _u.uuid4()
    pnl_poller._next_due_at[a] = 1e9
    pnl_poller._next_due_at[b] = 1e9
    pnl_poller.poll_now(a)
    assert a not in pnl_poller._next_due_at
    assert pnl_poller._next_due_at[b] == 1e9


def test_poll_now_is_safe_for_an_unknown_account():
    from app.services import pnl_poller

    pnl_poller.poll_now(uuid.uuid4())          # must not raise
