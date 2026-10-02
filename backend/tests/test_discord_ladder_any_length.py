"""The exit ladder as On Fill + any number of trims.

    On Fill   stop                      — set when the entry fills
    Trim 1…N  profit target, qty, stop  — as many as the trader adds

Trims 1–3 keep their original columns; later ones are JSON. The LAST trim, when
it is not 100%, rounds DOWN and leaves the balance as a runner.
"""
import uuid
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.discord_sources as ds
import app.services.discord_position_guard as guards
from app.models.order import OrderStatus
from app.schemas.discord import DiscordSettingsIn, DiscordTrimRow
from app.services import discord_auto_trim, discord_channel_settings as dcs, discord_ladder

USER = SimpleNamespace(id=uuid.uuid4())


def _ts(**kw):
    base = dict(
        discord_trim_profit_gate_pct=D("20"), discord_trim_stop_pct=D("-25"), discord_trim_qty_pct=D("50"),
        discord_trim2_profit_gate_pct=D("40"), discord_trim2_stop_pct=D("0"), discord_trim2_qty_pct=D("50"),
        discord_trim3_profit_gate_pct=D("60"), discord_trim3_stop_pct=D("10"), discord_trim3_qty_pct=D("100"),
        discord_trim_count=3, discord_extra_trims=[], discord_fill_stop_pct=None,
        discord_trim_price_threshold=D("99"), discord_trim_trail_amount=D("0.25"),
        discord_auto_trim=True, discord_manual_exit=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _row(gate, qty, stop):
    return DiscordTrimRow(profit_gate_pct=str(gate), qty_pct=str(qty), stop_pct=str(stop))


# ── reading and storing the ladder ───────────────────────────────────────────

def test_an_untouched_ladder_is_the_classic_three():
    r = discord_ladder.rungs(_ts())
    assert [(x.profit_gate_pct, x.qty_pct, x.stop_pct) for x in r] == [
        (D("20"), D("50"), D("-25")), (D("40"), D("50"), D("0")), (D("60"), D("100"), D("10"))]


def test_settings_that_predate_the_new_columns_still_read_as_three():
    bare = SimpleNamespace(discord_trim_profit_gate_pct=D("20"))      # nothing else set
    assert discord_ladder.count(bare) == 3
    assert [x.qty_pct for x in discord_ladder.rungs(bare)] == [D("50"), D("50"), D("100")]


def test_a_shorter_ladder_uses_only_its_first_trims():
    assert len(discord_ladder.rungs(_ts(discord_trim_count=1))) == 1
    assert discord_ladder.trim_config(_ts(discord_trim_count=2)).count == 2


def test_storing_five_trims_fills_the_columns_then_the_extras():
    ts = _ts()
    discord_ladder.store(ts, [(D(g), D(s), D(q)) for g, s, q in
                              [(10, -30, 25), (20, -10, 25), (30, 0, 25), (50, 10, 50), (80, 25, 100)]])
    assert ts.discord_trim_count == 5
    assert (ts.discord_trim3_profit_gate_pct, ts.discord_trim3_stop_pct, ts.discord_trim3_qty_pct) == (D(30), D(0), D(25))
    assert ts.discord_extra_trims == [
        {"profit_gate_pct": "50", "stop_pct": "10", "qty_pct": "50"},
        {"profit_gate_pct": "80", "stop_pct": "25", "qty_pct": "100"},
    ]
    r = discord_ladder.rungs(ts)
    assert [x.profit_gate_pct for x in r] == [D(10), D(20), D(30), D(50), D(80)]
    assert r[4].stop_pct == D(25)


def test_shrinking_the_ladder_drops_the_extras():
    ts = _ts(discord_trim_count=5, discord_extra_trims=[{"profit_gate_pct": "50"}, {"profit_gate_pct": "80"}])
    discord_ladder.store(ts, [(D(20), D(-25), D(50)), (D(40), D(0), D(100))])
    assert (ts.discord_trim_count, ts.discord_extra_trims) == (2, [])
    assert len(discord_ladder.rungs(ts)) == 2


def test_a_channel_keeps_its_own_ladder():
    account = _ts()
    own = dcs.ChannelSettings(account, {})
    discord_ladder.store(own, [(D(15), D(-20), D(100))])
    own.discord_fill_stop_pct = D("-30")
    again = dcs.ChannelSettings(account, own.to_json())          # as reloaded from the database
    assert discord_ladder.count(again) == 1 and discord_ladder.count(account) == 3
    assert discord_ladder.rungs(again)[0].profit_gate_pct == D(15)
    assert discord_ladder.fill_stop_pct(again) == D("-30") and discord_ladder.fill_stop_pct(account) is None


# ── the settings API ─────────────────────────────────────────────────────────

def test_the_settings_response_lists_every_trim_and_the_fill_stop():
    out = ds._settings_out(_ts(
        discord_trim_count=4, discord_fill_stop_pct=D("-25.0000"),
        discord_extra_trims=[{"profit_gate_pct": "90", "stop_pct": "30", "qty_pct": "100"}],
        discord_execution_mode="auto", discord_live_trading=True, discord_quantity_multiplier=1,
        discord_max_per_contract=None, discord_max_per_order=None, discord_trail_percent=D(20),
        discord_reprice_after_seconds=30, discord_reprice_pct=D(10),
    ))
    assert [(t.profit_gate_pct, t.qty_pct, t.stop_pct) for t in out.trims] == [
        ("20", "50", "-25"), ("40", "50", "0"), ("60", "100", "10"), ("90", "100", "30")]
    assert out.fill_stop_pct == "-25"


def test_patching_the_whole_ladder_and_the_fill_stop():
    ts = _ts()
    ds._apply_settings(ts, DiscordSettingsIn(
        trims=[_row(25, 50, -20), _row(50, 50, 0), _row(75, 50, 10), _row(100, 60, 25)],
        fill_stop_pct="-30",
    ), USER)
    assert discord_ladder.count(ts) == 4
    assert discord_ladder.rungs(ts)[3] == guards.RungConfig(D(100), D(25), D(60))
    assert ts.discord_fill_stop_pct == D("-30")


def test_an_empty_fill_stop_clears_it_and_an_unsent_one_is_left_alone():
    ts = _ts(discord_fill_stop_pct=D("-30"))
    ds._apply_settings(ts, DiscordSettingsIn(exit_mode="auto"), USER)
    assert ts.discord_fill_stop_pct == D("-30")
    ds._apply_settings(ts, DiscordSettingsIn(fill_stop_pct=""), USER)
    assert ts.discord_fill_stop_pct is None


@pytest.mark.parametrize("payload", [
    dict(fill_stop_pct="0"),                       # at entry: already through the market at fill
    dict(fill_stop_pct="10"),                      # above entry
    dict(fill_stop_pct="abc"),
    dict(trims=[_row(20, 150, -25)]),              # more than the whole position
    dict(trims=[_row(-5, 50, -25)]),               # a negative profit target
    dict(trims=[_row(20, 50, "x")]),
])
def test_bad_ladder_values_are_refused(payload):
    ts = _ts()
    with pytest.raises(HTTPException) as exc:
        ds._apply_settings(ts, DiscordSettingsIn(**payload), USER)
    assert exc.value.status_code == 400
    assert discord_ladder.count(ts) == 3 and ts.discord_fill_stop_pct is None


def test_a_ladder_needs_at_least_one_trim_and_at_most_ten():
    with pytest.raises(ValueError):
        DiscordSettingsIn(trims=[])
    with pytest.raises(ValueError):
        DiscordSettingsIn(trims=[_row(10, 10, 0)] * 11)


# ── the planner: any number of trims, and the runner ─────────────────────────

def _guard(rung=0, entry="1.00"):
    return SimpleNamespace(sell_count=rung, entry_price=D(entry), stop_price=None,
                           trail_qty=None, trail_amount=None, peak_price=None)


def _cfg(*rows, threshold="99"):
    return guards.TrimConfig(rungs=tuple(guards.RungConfig(D(g), D(s), D(q)) for g, q, s in rows),
                             price_threshold=D(threshold))


FIVE = _cfg((10, 25, -30), (20, 25, -10), (30, 25, 0), (50, 50, 10), (80, 100, 25))


def test_the_fourth_and_fifth_trims_use_their_own_settings():
    plan = guards.plan_exit(_guard(rung=3), D(8), D("1.60"), FIVE)       # trim 4: +60% >= 50%
    assert (plan.rung, plan.sell_qty, plan.new_stop_price) == (4, D(4), D("1.10"))
    plan = guards.plan_exit(_guard(rung=4), D(4), D("1.90"), FIVE)       # trim 5: 100%
    assert (plan.rung, plan.sell_qty, plan.retire) == (5, D(4), True)


def test_a_trim_before_the_last_still_rounds_up():
    plan = guards.plan_exit(_guard(rung=0), D(1), D("1.50"), FIVE)       # 25% of 1
    assert plan.sell_qty == D(1)


def test_the_last_trim_under_100_rounds_down_and_leaves_runners():
    cfg = _cfg((20, 50, -25), (40, 50, 0))                               # last trim is 50%
    plan = guards.plan_exit(_guard(rung=1), D(3), D("1.50"), cfg)
    assert plan.sell_qty == D(1)                                         # 1.5 rounds DOWN
    assert plan.retire is False and plan.new_stop_price == D("1.00")     # 2 runners, stop at break-even
    assert guards.RUNNER_NOTE in plan.note


def test_a_last_trim_that_rounds_down_to_nothing_sells_nothing():
    cfg = _cfg((20, 50, -25), (40, 50, 0))
    g = _guard(rung=1)
    plan = guards.plan_exit(g, D(1), D("1.50"), cfg)                     # 50% of 1
    assert plan.sell_qty == 0 and plan.retire is False
    assert guards.RUNNER_NOTE in plan.note and plan.new_stop_price == D("1.00")
    assert g.sell_count == 2                                             # the rung is spent


def test_a_last_trim_of_100_still_takes_everything():
    cfg = _cfg((20, 50, -25), (40, 100, 0))
    plan = guards.plan_exit(_guard(rung=1), D(3), D("1.50"), cfg)
    assert plan.sell_qty == D(3) and plan.retire is True


def test_an_alert_after_the_ladder_is_spent_repeats_the_last_trim():
    cfg = _cfg((20, 50, -25), (40, 50, 0))
    plan = guards.plan_exit(_guard(rung=2), D(4), D("1.50"), cfg)        # a third alert
    assert (plan.rung, plan.sell_qty) == (3, D(2))                       # last trim's 50%, rounded down


# ── auto-trim over a ladder of any length ────────────────────────────────────

def test_auto_trim_fires_a_fourth_trim_and_stops_after_the_last():
    ts = _ts(discord_trim_count=4,
             discord_extra_trims=[{"profit_gate_pct": "90", "stop_pct": "30", "qty_pct": "100"}])
    assert discord_auto_trim.due_rung(ts, _guard(rung=3), D("1.95")) == 4      # +95% >= 90%
    assert discord_auto_trim.due_rung(ts, _guard(rung=3), D("1.80")) is None   # not there yet
    assert discord_auto_trim.due_rung(ts, _guard(rung=4), D("5.00")) is None   # ladder spent


def test_auto_trim_on_a_one_trim_ladder_stops_after_it():
    ts = _ts(discord_trim_count=1)
    assert discord_auto_trim.due_rung(ts, _guard(rung=0), D("1.30")) == 1
    assert discord_auto_trim.due_rung(ts, _guard(rung=1), D("9.00")) is None


# ── the On Fill stop ─────────────────────────────────────────────────────────

class _DB:
    def __init__(self, order):
        self.order = order

    def get(self, model, key):
        return self.order


def _fill_guard(**kw):
    base = dict(fill_stop_done=False, closed_at=None, entry_order_id=uuid.uuid4(), sell_count=0,
                stop_price=None, entry_price=D("2.00"), symbol="SPY")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _no_fill_sync(monkeypatch):
    monkeypatch.setattr(guards, "sync_entry_price", lambda db, guard: False)


def _filled():
    return _DB(SimpleNamespace(status=OrderStatus.FILLED))


def test_the_stop_goes_on_when_the_entry_fills():
    g = _fill_guard()
    assert discord_auto_trim.apply_fill_stop(_filled(), g, _ts(discord_fill_stop_pct=D("-25"))) is True
    assert g.stop_price == D("1.50") and g.fill_stop_done is True


def test_nothing_happens_while_the_entry_is_still_working():
    g = _fill_guard()
    db = _DB(SimpleNamespace(status=OrderStatus.SUBMITTED))
    assert discord_auto_trim.apply_fill_stop(db, g, _ts(discord_fill_stop_pct=D("-25"))) is False
    assert g.stop_price is None and g.fill_stop_done is False            # looked at again next sweep


def test_it_is_decided_once_so_a_cancelled_stop_stays_cancelled():
    g = _fill_guard()
    ts = _ts(discord_fill_stop_pct=D("-25"))
    discord_auto_trim.apply_fill_stop(_filled(), g, ts)
    g.stop_price = None                                                  # the trader pulled it
    assert discord_auto_trim.apply_fill_stop(_filled(), g, ts) is False
    assert g.stop_price is None


def test_no_fill_stop_configured_sets_nothing_and_is_not_revisited():
    g = _fill_guard()
    assert discord_auto_trim.apply_fill_stop(_filled(), g, _ts()) is False
    assert g.stop_price is None and g.fill_stop_done is True
    # Configuring one later does not reach back to a position already open.
    assert discord_auto_trim.apply_fill_stop(_filled(), g, _ts(discord_fill_stop_pct=D("-25"))) is False


@pytest.mark.parametrize("ts, engine", [
    (_ts(discord_fill_stop_pct=D("-25"), discord_manual_exit=True), "ladder"),   # Manual exits: Kopyya never sells
    (_ts(discord_fill_stop_pct=D("-25")), "ai"),                                 # AI trimming runs the exits
])
def test_manual_exits_and_ai_trimming_get_no_fill_stop(ts, engine):
    g = _fill_guard()
    assert discord_auto_trim.apply_fill_stop(_filled(), g, ts, engine) is False
    assert g.stop_price is None and g.fill_stop_done is True


def test_a_stop_already_there_is_left_alone():
    g = _fill_guard(stop_price=D("1.80"))
    assert discord_auto_trim.apply_fill_stop(_filled(), g, _ts(discord_fill_stop_pct=D("-25"))) is False
    assert g.stop_price == D("1.80")


def test_a_holding_with_no_opening_order_of_ours_gets_none():
    g = _fill_guard(entry_order_id=None)
    assert discord_auto_trim.apply_fill_stop(_DB(None), g, _ts(discord_fill_stop_pct=D("-25"))) is False
    assert g.stop_price is None and g.fill_stop_done is True


def test_a_stop_that_rounds_to_zero_is_not_set():
    g = _fill_guard(entry_price=D("0.01"))
    assert discord_auto_trim.apply_fill_stop(_filled(), g, _ts(discord_fill_stop_pct=D("-90"))) is False
    assert g.stop_price is None
