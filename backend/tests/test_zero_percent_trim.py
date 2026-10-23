"""A trim that sells 0% is watched by price in Take-profit orders mode.

QA 2026-10-23, Clint QQQ 708C: the channel's ladder is T1 +20% sell 0% stop
+1%, T2 +50% sell 40% stop +10%, T3 +100% sell 67% stop +35%. A 0% trim has
nothing to rest as a take-profit, and in this mode the ladder only advanced
when one FILLED — so it never left the start. The position ran to +67% with
only the -35% On Fill stop; no stop lock, no T2.
"""
from decimal import Decimal as D

import app.services.discord_take_profit as tp
from test_discord_take_profit import NOW, _Book, _guard, _ts


def _clint(**kw):
    ladder = dict(discord_trim_profit_gate_pct=D("20"), discord_trim_qty_pct=D("0"), discord_trim_stop_pct=D("1"),
                  discord_trim2_profit_gate_pct=D("50"), discord_trim2_qty_pct=D("40"), discord_trim2_stop_pct=D("10"),
                  discord_trim3_profit_gate_pct=D("100"), discord_trim3_qty_pct=D("67"),
                  discord_trim3_stop_pct=D("35"))
    ladder.update(kw)
    return _ts(**ladder)


def _pass(book, g, held, mark, ts=None, now=NOW):
    return tp.reconcile(book, g, D(held), ts or _clint(), D(mark),
                        place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                        reconcile_stop=lambda: "in sync", now=now)


def test_below_the_target_nothing_moves():
    book, g = _Book(), _guard(entry_price=D("1.07"), stop_price=D("0.69"))
    _pass(book, g, 2, "1.20")
    assert (g.sell_count, g.stop_price) == (0, D("0.69")) and book.calls == []


def test_at_the_target_the_stop_locks_and_trim_two_rests_at_once():
    """Clint's QQQ 708C at 1.30 (+21%): T1 passes, the stop goes to +1% (1.08),
    and T2's take-profit — 40% of 2 = 0.8, rounded up to 1 — rests at +50%."""
    book, g = _Book(), _guard(entry_price=D("1.07"), stop_price=D("0.69"))
    out = _pass(book, g, 2, "1.30")
    assert g.sell_count == 1
    assert g.stop_price == D("1.08"), "+1% of 1.07, to the tick"
    assert book.calls == [("pair", D(1), D("1.61"), D("1.08"))]
    assert "trim 1 (0%) passed" in out


def test_a_higher_stop_already_set_is_never_lowered():
    """A trailing stop the trader armed by hand sat at 1.32 — the +1% lock (1.08)
    must not pull it down."""
    book, g = _Book(), _guard(entry_price=D("1.07"), stop_price=D("1.32"))
    _pass(book, g, 2, "1.66")
    assert g.sell_count == 1 and g.stop_price == D("1.32")


def test_several_zero_percent_trims_in_a_row_are_all_passed():
    ts = _clint(discord_trim2_qty_pct=D("0"))
    book, g = _Book(), _guard(entry_price=D("1.00"), stop_price=D("0.65"))
    _pass(book, g, 4, "1.60", ts=ts)                       # past +20% and +50%
    assert g.sell_count == 2 and g.stop_price == D("1.10")
    assert book.calls[-1][0:3] == ("pair", D(3), D("2.00"))  # T3: 67% of 4 = 2.68 -> 3, at +100%


def test_a_zero_percent_trim_with_no_target_waits_for_its_alert():
    ts = _clint(discord_trim_profit_gate_pct=D("0"))
    book, g = _Book(), _guard(entry_price=D("1.07"), stop_price=D("0.69"))
    _pass(book, g, 2, "1.66", ts=ts)
    assert g.sell_count == 0 and book.calls == []


def test_a_real_first_trim_is_left_to_its_take_profit():
    """Only a 0% trim is watched: an ordinary one still advances on its fill."""
    book, g = _Book(), _guard(entry_price=D("1.00"), stop_price=D("0.75"))
    _pass(book, g, 4, "1.30", ts=_ts())
    assert g.sell_count == 0
    assert book.calls and book.calls[0][0] == "limit"       # T1's take-profit, at the market already
