"""AI trimming: an OpenRouter model decides a Discord position's exit.

The model's reply is advisory input to our own rules. These tests pin the
rules: what it may do (hold / trim / exit / raise the stop), what it may never
do (buy, oversell, lower a stop, set one at or above the price), when it is
asked at all, and that suggest mode never trades.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import ai_trim
from app.services import discord_auto_trim as at

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)


# ── validate: the model is held to four actions ─────────────────────────────

def _v(raw, held="4", mark="2.00", stop=None):
    return ai_trim.validate(raw, D(held), D(mark), D(stop) if stop else None)


def test_a_trim_sells_its_share_of_what_is_held_rounded_up():
    d = _v({"action": "trim", "sell_pct": 30, "stop_price": None, "reason": "r"})
    assert (d.action, d.sell_qty) == ("trim", D(2))     # 30% of 4 -> 1.2 -> 2


def test_a_trim_of_everything_is_an_exit():
    d = _v({"action": "trim", "sell_pct": 250, "stop_price": None, "reason": ""})
    assert (d.action, d.sell_qty) == ("exit", D(4))
    assert "capped at 100" in d.notes[0]


def test_exit_sells_everything_and_ignores_a_stop():
    d = _v({"action": "exit", "sell_pct": 0, "stop_price": 1.5, "reason": ""})
    assert (d.action, d.sell_qty, d.new_stop) == ("exit", D(4), None)


def test_a_stop_only_moves_up():
    d = _v({"action": "raise_stop", "sell_pct": 0, "stop_price": 1.2, "reason": ""}, stop="1.40")
    assert d.action == "hold" and d.new_stop is None
    assert "would not raise" in d.notes[0]


def test_a_stop_at_or_above_the_price_is_refused():
    d = _v({"action": "raise_stop", "sell_pct": 0, "stop_price": 2.00, "reason": ""})
    assert d.action == "hold"
    assert "at or above the price" in d.notes[0]


def test_a_valid_stop_is_rounded_down_to_the_cent():
    d = _v({"action": "raise_stop", "sell_pct": 0, "stop_price": 1.678, "reason": ""}, stop="1.00")
    assert (d.action, d.new_stop) == ("raise_stop", D("1.67"))


def test_a_trim_can_raise_the_stop_on_the_rest():
    d = _v({"action": "trim", "sell_pct": 50, "stop_price": 1.8, "reason": ""})
    assert (d.action, d.sell_qty, d.new_stop) == ("trim", D(2), D("1.80"))


@pytest.mark.parametrize("raw", [
    {"action": "buy", "sell_pct": 100, "stop_price": None, "reason": ""},
    {"action": "trim", "sell_pct": "lots", "stop_price": None, "reason": ""},
    "not an object",
    None,
])
def test_anything_else_is_a_hold(raw):
    assert _v(raw).action == "hold"
    assert _v(raw).sell_qty == 0


def test_a_reply_wrapped_in_prose_still_parses():
    assert ai_trim.parse_reply('Sure:\n```json\n{"action": "hold"}\n```') == {"action": "hold"}
    with pytest.raises(ai_trim.ModelError):
        ai_trim.parse_reply("I think you should hold.")


def test_the_openrouter_title_header_is_ascii():
    """A non-ASCII header value makes httpx raise before the request is sent."""
    ai_trim._TITLE.encode("ascii")


# ── due: when the model is asked ────────────────────────────────────────────

def test_the_first_look_is_always_due():
    assert ai_trim.due(None, None, D("2"), NOW, D(5), 60)


def test_not_again_inside_the_minimum_interval_however_far_it_moved():
    assert not ai_trim.due(D("2"), NOW - timedelta(seconds=30), D("3"), NOW, D(5), 60)


def test_again_once_the_price_has_moved_enough():
    last = NOW - timedelta(minutes=5)
    assert ai_trim.due(D("2.00"), last, D("2.10"), NOW, D(5), 60)       # +5%
    assert not ai_trim.due(D("2.00"), last, D("2.09"), NOW, D(5), 60)   # +4.5%
    assert ai_trim.due(D("2.00"), last, D("1.90"), NOW, D(5), 60)       # -5%


# ── consider / execute ──────────────────────────────────────────────────────

class _Db:
    def __init__(self):
        self.added, self.commits = [], 0

    def add(self, row):
        row.id = row.id or uuid.uuid4()
        row.created_at = NOW
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def execute(self, *a, **k):
        return None


def _setup(monkeypatch, mode="auto", live=True):
    guard = SimpleNamespace(
        id=uuid.uuid4(), symbol="SPY", option_strike=D("500"), option_right="call",
        option_expiry=date(2026, 10, 2), entry_price=D("2.00"), stop_price=None,
        stop_order_id=None, closed_at=None,
    )
    pos = SimpleNamespace(symbol="SPY", option_strike=D("500"), option_right="call",
                          option_expiry=date(2026, 10, 2), quantity=D("4"))
    ts = SimpleNamespace(discord_ai_mode=mode, discord_live_trading=live,
                         discord_ai_model="m", discord_ai_move_pct=D(5),
                         discord_ai_min_interval_s=60, discord_ai_instructions=None)
    user = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(ai_trim, "_recent", lambda db, g, n=3: [])
    monkeypatch.setattr(ai_trim, "_has_working_exit", lambda db, g: False)
    monkeypatch.setattr(ai_trim, "_supersede_pending", lambda db, g, keep: None)
    monkeypatch.setattr(ai_trim, "build_messages", lambda *a, **k: [])
    sold = []
    import app.services.pnl_poller as poller
    monkeypatch.setattr(poller, "place_exit",
                        lambda db, u, acct, ad, p, q, partial=False:
                        sold.append((q, partial)) or SimpleNamespace(id=uuid.uuid4()))
    return guard, pos, ts, user, sold


def _answer(**kw):
    raw = {"action": "trim", "sell_pct": 50, "stop_price": 2.2, "reason": "lock half"}
    raw.update(kw)
    return lambda model, messages: raw


def test_auto_mode_sells_and_raises_the_stop(monkeypatch):
    guard, pos, ts, user, sold = _setup(monkeypatch)
    row = ai_trim.consider(_Db(), user, ts, None, None, guard, pos, D("2.60"),
                           now=NOW, ask_fn=_answer())
    assert row.status == "executed"
    assert sold == [(D(2), True)]          # half of 4, and a partial close
    assert guard.stop_price == D("2.20")


def test_suggest_mode_never_trades(monkeypatch):
    guard, pos, ts, user, sold = _setup(monkeypatch, mode="suggest")
    row = ai_trim.consider(_Db(), user, ts, None, None, guard, pos, D("2.60"),
                           now=NOW, ask_fn=_answer())
    assert row.status == "suggested"
    assert sold == [] and guard.stop_price is None


def test_without_live_trading_auto_is_paper(monkeypatch):
    guard, pos, ts, user, sold = _setup(monkeypatch, live=False)
    row = ai_trim.consider(_Db(), user, ts, None, None, guard, pos, D("2.60"),
                           now=NOW, ask_fn=_answer())
    assert row.status == "paper"
    assert sold == [] and guard.stop_price is None


def test_an_unreachable_model_is_recorded_and_holds(monkeypatch):
    guard, pos, ts, user, sold = _setup(monkeypatch)

    def boom(model, messages):
        raise ai_trim.ModelError("OPENROUTER_API_KEY is not set on the server")

    row = ai_trim.consider(_Db(), user, ts, None, None, guard, pos, D("2.60"), now=NOW, ask_fn=boom)
    assert row.status == "error" and "OPENROUTER_API_KEY" in row.notes
    assert sold == []


def test_a_working_ai_exit_blocks_the_next_ask(monkeypatch):
    guard, pos, ts, user, sold = _setup(monkeypatch)
    monkeypatch.setattr(ai_trim, "_has_working_exit", lambda db, g: True)
    asked = []
    assert ai_trim.consider(_Db(), user, ts, None, None, guard, pos, D("2.60"), now=NOW,
                            ask_fn=lambda m, msgs: asked.append(1)) is None
    assert asked == []


# ── the engines are either/or ───────────────────────────────────────────────

def test_the_ai_engine_replaces_the_ladder_sweep():
    import inspect

    src = inspect.getsource(at._sweep_trader)
    assert 'engine == "ladder" and not _enabled(ts)' in src
    ai_branch = src[src.index('if engine == "ai":'):]
    assert "ai_trim.sweep(" in ai_branch
    # ...and returns before the ladder's rung loop can run.
    assert ai_branch.index("return") < ai_branch.index("for guard in rows:\n        try:")


def test_engine_defaults_to_the_ladder():
    assert at._engine(None) == "ladder"
    assert at._engine(SimpleNamespace(discord_exit_engine="ai")) == "ai"
    assert at._engine(SimpleNamespace(discord_exit_engine="bogus")) == "ladder"


# ── the model dropdown ──────────────────────────────────────────────────────

def test_only_real_time_structured_output_models_are_offered(monkeypatch):
    """ask() sends a strict json_schema; a model without structured outputs
    answers in prose, which is a HOLD every time. Batch variants aren't live."""
    import httpx

    payload = {"data": [
        {"id": "a/good", "name": "A: Good", "supported_parameters": ["structured_outputs"],
         "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
        {"id": "a/good:batch", "name": "A: Good (batch)", "supported_parameters": ["structured_outputs"]},
        {"id": "b/prose", "name": "B: Prose", "supported_parameters": ["temperature"]},
    ]}
    monkeypatch.setattr(ai_trim, "_models_cache", None)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: payload))

    assert ai_trim.list_models() == [
        {"id": "a/good", "name": "A: Good", "prompt_per_m": "2", "completion_per_m": "10"},
    ]
