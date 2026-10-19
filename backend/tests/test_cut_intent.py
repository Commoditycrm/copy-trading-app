"""Only a committed "cut" sells.

The real messages (tests/fixtures/discord_cut_messages.json) are every "cut" the
Rafael, Clint and Breakdownsniper channels posted on QA up to 2026-10-16,
labelled by hand: 10 real cuts, 14 not. Reading any "cut" as a full close sold
on six of the fourteen and bought on one of the ten.
"""
import json
import os
import sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage
from app.services.discord_parsers.cut_intent import BLOCKED, CUT, NONE, PARTIAL, classify

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "discord_cut_messages.json")
REAL = json.load(open(_FIXTURE, encoding="utf-8"))


def _id(row):
    return f"{row['channel']}-{row['posted_at']}-{row['kind']}"


# ── every real message, through the whole parser ────────────────────────────

@pytest.mark.parametrize("row", REAL, ids=[_id(r) for r in REAL])
def test_real_messages(row):
    res = parse_message(ParsedMessage(content=row["content"], author=row["channel"]))
    sells = [s for s in (res.signals or []) if s.action.value == "SELL"]
    buys = [s for s in (res.signals or []) if s.action.value == "BUY"]
    assert not buys, f"a cut message must never buy: {row['content'][:80]}"
    if row["label"] == "cut":
        assert sells, f"missed a real cut: {row['content'][:80]} ({res.reason})"
    else:
        assert not sells, f"sold on talk about cutting: {row['content'][:80]}"


def test_the_set_is_what_it_says():
    assert len(REAL) == 24
    assert sum(r["label"] == "cut" for r in REAL) == 10


# ── each rule, on its own ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Cutting", "Cut AMZN", "Cutting here", "Cut it", "cutting the rest", "Cut my SPY puts here.",
    "I'm cutting QQQ", "Cutting @here 10% loss", "Cut AMZN -30%", "Cutting for a loss",
    "Will cut here", "Going to cut remaining position here @ 2.6", "Cutting spy calls",
    "Cutting @here break even", "I've cut my SPY calls", "cut rest of spy",
])
def test_committed_cuts(text):
    assert classify(text).kind == CUT, classify(text)


@pytest.mark.parametrize("text, reason", [
    ("I may cut", "hedged"), ("might cut SPY", "hedged"), ("thinking about cutting", "hedged"),
    ("going to have to cut calls", "hedged"), ("gonna cut", "hedged"), ("will cut soon", "conditional"),
    ("cut if it breaks 720", "conditional"), ("will cut below .50", "conditional"),
    ("If it loses VWAP I'm cutting", "conditional"), ("not cutting yet", "negated"),
    ("won't cut this one", "negated"), ("didn't cut for break even", "negated"),
    ("should I cut?", "question"), ("cut here?", "question"),
    ("those SPY we cut earlier went to .75", "past"), ("if I had cut it", "conditional"),
    ("rather than cutting it", "past"), ("already cut that one", "past"),
    ("Cut if you don't feel like risking it", "advice"), ("cut losers quickly", "advice"),
    ("we must cut losers", "advice"),
])
def test_talk_about_cutting_does_not_sell(text, reason):
    v = classify(text)
    assert v.kind == BLOCKED and v.reason == reason, v


@pytest.mark.parametrize("text, fraction", [
    ("cut half", D("0.5")), ("Cutting half here", D("0.5")), ("cut 1/2", D("0.5")),
    ("cutting a third", D(1) / 3), ("cut 1/3 SPY", D(1) / 3), ("cut two thirds", D(2) / 3),
    ("cut 30%", D("0.3")), ("cutting some here", D("0.5")),
])
def test_partial_cuts(text, fraction):
    v = classify(text)
    assert v.kind == PARTIAL and v.fraction == fraction, v


@pytest.mark.parametrize("text", ["Cutting @here 10% loss", "Cut AMZN -30%", "cutting 44% loss"])
def test_a_percent_that_is_the_result_is_not_a_size(text):
    assert classify(text).kind == CUT


def test_no_cut_word_is_none():
    assert classify("Trimming 50% here").kind == NONE


def test_a_long_message_needs_a_plain_trade_clause():
    """A paragraph that mentions cutting in passing is commentary; one with
    "Cutting my $SPY calls here" in it is a cut."""
    filler = "The market chopped all morning and the open was ugly. " * 8
    assert classify(filler + "Cutting quickly matters.").kind == BLOCKED
    assert classify(filler + "Cutting my $SPY calls here.").kind == CUT


# ── what the parser does with them ──────────────────────────────────────────

def _parse(text):
    return parse_message(ParsedMessage(content=text, author="test"))


def test_a_cut_closes_the_named_ticker_in_full():
    s = _parse("Cut my SPY puts here.").signals[0]
    assert (s.action.value, s.symbol, s.option_type.value) == ("SELL", "SPY", "PUT")
    assert s.close_all_matching and s.flatten


def test_a_lower_case_ticker_before_calls_is_the_ticker():
    assert _parse("Cutting spy calls").signals[0].symbol == "SPY"


def test_the_word_I_is_never_a_ticker():
    s = _parse("Cutting of course I trade when not feeling well").signals[0]
    assert s.symbol is None and s.latest_contract


def test_a_partial_cut_is_a_partial_close_with_its_fraction():
    s = _parse("cut half AMZN here").signals[0]
    assert s.is_partial_close and not s.flatten and s.cut_fraction == D("0.5")
    assert s.symbol == "AMZN" and s.as_dict()["cut_fraction"] == "0.5"


def test_talk_about_cutting_is_ignored_with_the_reason():
    res = _parse("Will cut if we close below my downside test level")
    assert res.status.value == "ignored" and not res.signals


def test_a_cut_is_never_read_as_an_entry():
    res = _parse("@Rafael the 7800 long is weak. Going to cut remaining position here @ 2.6")
    assert all(s.action.value == "SELL" for s in res.signals)


# ── a stop loss that was hit is a cut (the trader's call, 2026-10-19) ───────

from app.services.discord_parsers.cut_intent import stop_hit  # noqa: E402


@pytest.mark.parametrize("text", [
    "SL Hit, -35%\n@Julia", "SL hit", "SL got hit", "Stop loss hit -40%", "stop-loss hit",
    "Stop hit on SPY", "Hit my stop", "hit SL", "SL triggered on QQQ puts", "Stopped out",
    "Stopped out of rest of SPY calls",
])
def test_a_stop_loss_hit_is_a_cut(text):
    assert stop_hit(text).kind == CUT
    s = _parse(text).signals[0]
    assert s.action.value == "SELL" and s.source_action == "CUTTING" and s.close_all_matching


@pytest.mark.parametrize("text, reason", [
    ("SL hit?", "question"), ("If SL hit I'm out", "conditional"), ("SL hit, not yet", "negated"),
    ("Almost hit my stop", "hedged"), ("Got stopped out yesterday", "past"),
])
def test_talk_about_a_stop_loss_is_not_a_cut(text, reason):
    v = stop_hit(text)
    assert v.kind == BLOCKED and v.reason == reason, v
    assert not [s for s in (_parse(text).signals or []) if s.action.value == "SELL"]


def test_setting_a_stop_is_not_a_hit():
    assert stop_hit("SL at 0.20").kind == NONE
    assert stop_hit("SL not hit yet").kind == NONE
    assert not _parse("SL not hit yet").signals


def test_sl_is_never_the_ticker():
    assert _parse("SL Hit, -35%").signals[0].symbol is None          # the channel's position
    s = _parse("SL triggered on QQQ puts").signals[0]
    assert (s.symbol, s.option_type.value) == ("QQQ", "PUT")
