"""A ladder row's stop can TRAIL instead of sitting at a fixed level.

Fixed: a return from entry (-25 = 25% below entry), as before.
Trail: a give-back from the best price since that row (15 = 15% below the
high). It starts that far below the price when the row fires and is only ever
raised, so the stop resting at the broker follows the position up.
"""
import uuid
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.models.order import OrderStatus
from app.services import discord_auto_trim, discord_ladder
from app.services import discord_position_guard as guards
from app.services import discord_take_profit, discord_trailing_stop
from app.services.discord_channel_settings import ChannelSettings


def _ts(**kw):
    base = dict(discord_trim_count=3, discord_extra_trims=[], discord_stop_trails=None,
                discord_trim_profit_gate_pct=D(20), discord_trim_stop_pct=D(-25), discord_trim_qty_pct=D(50),
                discord_trim2_profit_gate_pct=D(0), discord_trim2_stop_pct=D(15), discord_trim2_qty_pct=D(50),
                discord_trim3_profit_gate_pct=D(0), discord_trim3_stop_pct=D(0), discord_trim3_qty_pct=D(100),
                discord_fill_stop_pct=None, discord_manual_exit=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _guard(**kw):
    base = dict(symbol="SPY", sell_count=0, entry_price=D("2.00"), stop_price=None, stop_trail_pct=None, stop_peak=None,
                stop_order_id=None, tp_stop_order_id=None, trail_qty=None, trail_amount=None, peak_price=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── settings ─────────────────────────────────────────────────────────────────

def test_rows_read_their_trail_flag():
    ts = _ts(discord_stop_trails={"fill": True, "trims": [False, True]})
    assert [r.stop_trail for r in discord_ladder.rungs(ts)] == [False, True, False]
    assert discord_ladder.fill_stop_trails(ts) is True


def test_no_flags_means_every_stop_is_fixed():
    ts = _ts()
    assert not any(r.stop_trail for r in discord_ladder.rungs(ts))
    assert discord_ladder.fill_stop_trails(ts) is False


def test_storing_the_ladder_writes_the_flags_and_keeps_the_fill_one():
    ts = _ts(discord_stop_trails={"fill": True})
    discord_ladder.store(ts, [(D(20), D(-25), D(50)), (D(0), D(15), D(100))], [False, True])
    assert ts.discord_stop_trails == {"fill": True, "trims": [False, True]}


def test_a_channel_whose_copy_predates_trailing_reads_fixed_stops():
    """Not the account's flags: a channel's own ladder stays its own."""
    account = _ts(discord_stop_trails={"fill": True, "trims": [True, True, True]})
    ch = ChannelSettings(account, {"discord_trim_count": 3})
    assert discord_ladder.fill_stop_trails(ch) is False


def test_the_configured_ladder_no_longer_trails_trims_out():
    assert discord_ladder.trim_config(_ts()).trail_exits is False


# ── a trim sets the stop ─────────────────────────────────────────────────────

def test_a_trailing_trim_starts_its_stop_below_the_price_now():
    ts = _ts(discord_stop_trails={"trims": [False, True]})
    guard = _guard(sell_count=1, stop_price=D("1.50"))
    plan = guards.plan_exit(guard, D(2), D("3.00"), discord_ladder.trim_config(ts))
    guards.apply_stop(guard, plan)
    assert plan.sell_qty == D(1)
    assert guard.stop_price == D("2.55")          # 15% under 3.00
    assert guard.stop_trail_pct == D(15) and guard.stop_peak == D("3.00")


def test_a_fixed_trim_after_a_trailing_one_stops_trailing():
    ts = _ts()
    guard = _guard(sell_count=1, stop_price=D("2.55"), stop_trail_pct=D(15), stop_peak=D("3.00"))
    cfg = discord_ladder.trim_config(ts)
    cfg = guards.TrimConfig(rungs=(cfg.rungs[0], guards.RungConfig(D(0), D(10), D(50))), trail_exits=False)
    guards.apply_stop(guard, guards.plan_exit(guard, D(2), D("3.00"), cfg))
    assert guard.stop_price == D("2.20") and guard.stop_trail_pct is None and guard.stop_peak is None


# ── it follows the price up, never down ──────────────────────────────────────

def test_the_stop_rises_with_a_new_high():
    guard = _guard(stop_price=D("2.55"), stop_trail_pct=D(15), stop_peak=D("3.00"))
    assert guards.ratchet_stop(guard, D("3.50")) is True
    assert guard.stop_peak == D("3.50") and guard.stop_price == D("2.97")


def test_a_pullback_leaves_it_where_it_is():
    guard = _guard(stop_price=D("2.97"), stop_trail_pct=D(15), stop_peak=D("3.50"))
    assert guards.ratchet_stop(guard, D("3.10")) is False
    assert guard.stop_price == D("2.97") and guard.stop_peak == D("3.50")


def test_a_tiny_rise_does_not_churn_the_resting_order():
    guard = _guard(stop_price=D("2.55"), stop_trail_pct=D(15), stop_peak=D("3.00"))
    assert guards.ratchet_stop(guard, D("3.02")) is False     # 2.567 — under the 2% step
    assert guard.stop_price == D("2.55") and guard.stop_peak == D("3.02")


def test_a_fixed_stop_never_moves():
    guard = _guard(stop_price=D("1.50"))
    assert guards.ratchet_stop(guard, D("9.00")) is False and guard.stop_price == D("1.50")


def test_the_poller_ratchets_before_judging_the_stop():
    guard = _guard(stop_price=D("2.55"), stop_trail_pct=D(15), stop_peak=D("3.00"))
    assert discord_trailing_stop.decide(guard, D("4.00"), D(2)) is None
    assert guard.stop_price == D("3.40")
    kind, qty, level = discord_trailing_stop.decide(guard, D("3.30"), D(2))
    assert kind == discord_trailing_stop.STOP and level == D("3.40")


# ── On Fill ──────────────────────────────────────────────────────────────────

def _filled_db(monkeypatch):
    monkeypatch.setattr(guards, "sync_entry_price", lambda db, g: None)
    order = SimpleNamespace(status=OrderStatus.FILLED)
    return SimpleNamespace(get=lambda model, oid: order)


def test_a_trailing_on_fill_stop_trails_from_the_fill(monkeypatch):
    db = _filled_db(monkeypatch)
    ts = _ts(discord_fill_stop_pct=D(20), discord_stop_trails={"fill": True})
    guard = _guard(entry_order_id=uuid.uuid4(), fill_stop_done=False, closed_at=None)
    assert discord_auto_trim.apply_fill_stop(db, guard, ts) is True
    assert guard.stop_price == D("1.60") and guard.stop_trail_pct == D(20) and guard.stop_peak == D("2.00")


def test_a_fixed_on_fill_stop_is_unchanged(monkeypatch):
    db = _filled_db(monkeypatch)
    ts = _ts(discord_fill_stop_pct=D(-25))
    guard = _guard(entry_order_id=uuid.uuid4(), fill_stop_done=False, closed_at=None)
    assert discord_auto_trim.apply_fill_stop(db, guard, ts) is True
    assert guard.stop_price == D("1.50") and guard.stop_trail_pct is None


# ── a take-profit fill ───────────────────────────────────────────────────────

def test_a_take_profit_fill_starts_a_trailing_stop_from_its_fill_price():
    ts = _ts(discord_stop_trails={"trims": [True]})
    tp = SimpleNamespace(id=uuid.uuid4(), status=OrderStatus.FILLED, filled_quantity=D(1),
                         filled_avg_price=D("2.40"), limit_price=D("2.40"))
    db = SimpleNamespace(get=lambda model, oid: tp if oid == tp.id else None)
    guard = _guard(tp_order_id=tp.id, tp_stop_order_id=None, tp_rung=1, tp_qty=D(1))
    discord_take_profit.settle(db, guard, ts, cancel=lambda oid: None)
    # trim 1's -25 read as a 25% give-back from 2.40
    assert guard.stop_price == D("1.80") and guard.stop_trail_pct == D(25) and guard.stop_peak == D("2.40")


# ── the settings API ─────────────────────────────────────────────────────────

def _patch(ts, **body):
    from app.api.discord_sources import _apply_settings
    from app.schemas.discord import DiscordSettingsIn

    _apply_settings(ts, DiscordSettingsIn(**body), SimpleNamespace(id=uuid.uuid4()))
    return ts


def test_saving_a_trailing_row_stores_a_positive_give_back():
    ts = _patch(_ts(), trims=[
        {"profit_gate_pct": "33", "qty_pct": "50", "stop_pct": "-25"},
        {"profit_gate_pct": "75", "qty_pct": "75", "stop_pct": "-15", "stop_trail": True},
    ])
    assert ts.discord_trim2_stop_pct == D(15)
    assert ts.discord_stop_trails["trims"] == [False, True]


@pytest.mark.parametrize("bad", ["0", "100", "0.5"])
def test_a_trailing_give_back_must_be_1_to_99(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _patch(_ts(), trims=[{"profit_gate_pct": "0", "qty_pct": "100", "stop_pct": bad, "stop_trail": True}])


def test_a_trailing_on_fill_stop_takes_a_positive_give_back():
    ts = _patch(_ts(), fill_stop_pct="20", fill_stop_trail=True)
    assert ts.discord_fill_stop_pct == D(20) and ts.discord_stop_trails["fill"] is True
    ts = _patch(ts, fill_stop_pct="-25", fill_stop_trail=False)     # back to fixed
    assert ts.discord_fill_stop_pct == D(-25) and ts.discord_stop_trails["fill"] is False


def test_the_settings_read_back_the_flags():
    from app.api.discord_sources import _settings_out
    from app.models.settings import TraderSettings

    ts = _patch(TraderSettings(user_id=uuid.uuid4(), discord_trim_count=3, discord_extra_trims=[]), fill_stop_pct="20", fill_stop_trail=True, trims=[
        {"profit_gate_pct": "33", "qty_pct": "50", "stop_pct": "15", "stop_trail": True}])
    out = _settings_out(ts)
    assert out.fill_stop_trail is True and out.trims[0].stop_trail is True and out.trims[0].stop_pct == "15"
