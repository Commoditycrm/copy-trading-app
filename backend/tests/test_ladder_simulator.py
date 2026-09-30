"""The Simulated Prices dry run: the ladder narrated along a price path.

It exists so a trader can see what the ladder would do while the market is
closed, so it must run the REAL rules — auto_trim.due_rung, plan_exit and the
stop enforcer's decide() — and must never place or write anything.
"""
import inspect
import os
import sys
from decimal import Decimal as D
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import discord_position_guard as guards
from app.services import ladder_simulator as sim


def _ts(auto=True, g=("20", "35", "60")):
    return SimpleNamespace(
        discord_auto_trim=auto,
        discord_trim_profit_gate_pct=D(g[0]),
        discord_trim2_profit_gate_pct=D(g[1]),
        discord_trim3_profit_gate_pct=D(g[2]),
    )


def _cfg(threshold="0.90"):
    return guards.TrimConfig(
        trim1=guards.RungConfig(D("20"), D("-25"), D("50")),
        trim2=guards.RungConfig(D("35"), D("0"), D("50")),
        trim3=guards.RungConfig(D("60"), D("0"), D("100")),
        price_threshold=D(threshold),
        trail_amount=D("0.25"),
    )


def _kinds(steps):
    return [[e.kind for e in s.events] for s in steps]


def _run(pcts, entry="0.50", qty="4", ts=None, cfg=None):
    return sim.simulate(ts or _ts(), cfg or _cfg(), D(entry), D(qty), [D(p) for p in pcts])


def test_trims_fire_at_their_gates_and_close_the_position():
    """A cheap contract (under the trail threshold) sells every rung at market."""
    steps = _run(["0", "10", "25", "40", "65"])
    assert _kinds(steps) == [["hold"], ["hold"], ["trim"], ["trim"], ["trim"]]
    assert [s.held for s in steps] == [D(4), D(4), D(2), D(1), D(0)]
    assert "Trim 1 fires at +20%" in steps[1].events[0].text
    assert "Position closed" in steps[-1].events[0].text


def test_the_first_trim_sets_the_configured_stop():
    steps = _run(["25"])
    assert steps[0].stop == D("0.37")    # 0.50 x 0.75, rounded down to the cent


def test_falling_through_the_stop_closes_everything():
    steps = _run(["25", "-30"])
    assert _kinds(steps)[1] == ["stop_hit"]
    assert steps[1].held == 0


def test_an_expensive_contract_rides_a_trail_then_gives_back():
    """Above the price threshold, trim 2 arms a trailing exit instead of selling."""
    steps = _run(["25", "40", "50", "20"], entry="2.00")
    assert _kinds(steps)[1] == ["trail_armed"]
    assert steps[1].held == D(2)    # nothing sold yet
    assert _kinds(steps)[3][0] == "trail_hit"


def test_an_ungated_rung_is_explained_not_fired():
    """A gate of 0 never auto-fires; live it waits for a Discord alert."""
    steps = _run(["25", "90"], ts=_ts(g=("20", "0", "0")))
    assert _kinds(steps)[1] == ["hold"]
    assert "waits for a Discord alert" in steps[1].events[0].text


def test_auto_trim_off_still_simulates():
    steps = _run(["25"], ts=_ts(auto=False))
    assert _kinds(steps) == [["trim"]]


def test_it_places_nothing_and_writes_nothing():
    src = inspect.getsource(sim)
    for forbidden in ("submit_self_alert_text", "_place_trader_order", "SessionLocal",
                      "db.commit", "adapter"):
        assert forbidden not in src, forbidden


def test_it_uses_the_live_rules_not_a_copy():
    src = inspect.getsource(sim.simulate)
    assert "protections.decide(" in src
    assert "auto_trim.due_rung(" in src
    assert "guards.plan_exit(" in src
