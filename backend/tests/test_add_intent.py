"""Only a committed "add" averages in — and one naming no ticker or price works.

The real messages (tests/fixtures/discord_add_messages.json) are every "add" the
Julia, Clint, Breakdownsniper and Mark channels posted on QA up to 2026-10-19,
labelled by hand: 19 adds, 6 entries that only mention adding ("Leave room to
add"), 11 that are talk. Before this, the no-ticker / no-price adds were ignored
(Julia's "Added to my position", Mark's "Added", "Added here", "Last Add") and
two pieces of talk were read as entries (BUY "THIS", BUY "I").
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services.discord_parsers import parse_message
from app.services.discord_parsers.add_intent import ADD, BLOCKED, NONE, classify
from app.services.discord_parsers.base import ParsedMessage

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "discord_add_messages.json")
REAL = json.load(open(_FIXTURE, encoding="utf-8"))


def _parse(text, at="2026-10-19T13:46"):
    return parse_message(ParsedMessage(
        content=text, posted_at=datetime.fromisoformat(at).replace(tzinfo=timezone.utc)))


@pytest.mark.parametrize("row", REAL, ids=[f"{r['channel']}-{r['posted_at']}-{r['label']}" for r in REAL])
def test_real_messages(row):
    res = _parse(row["content"], row["posted_at"])
    sig = (res.signals or [None])[0]
    if row["label"] == "add":
        assert sig is not None and sig.action.value == "BUY" and (sig.double_up or sig.add_to_latest), \
            f"missed an add: {row['content'][:80]} ({res.reason})"
        if row.get("symbol"):
            assert sig.symbol == row["symbol"]
    elif row["label"] == "entry":
        assert sig is not None and sig.action.value == "BUY" and not sig.add_to_latest
    else:
        assert sig is None, f"acted on talk: {row['content'][:80]}"


def test_the_set_is_what_it_says():
    assert len(REAL) == 36 and sum(r["label"] == "add" for r in REAL) == 19


@pytest.mark.parametrize("text, symbol", [
    ("Added", None), ("Added here", None), ("Added to my position", None), ("Last Add", None),
    ("This is my last add here.", None), ("Added to lotto", None), ("I've added more here", None),
    ("Added to Meta", "META"), ("Added to $iwm", "IWM"), ("Adding a couple more on original TSLA entry", "TSLA"),
    ("Added to SPY, not heavy", "SPY"), ("Adding here, you should too", None),
])
def test_committed_adds(text, symbol):
    v = classify(text)
    assert v.kind == ADD and v.symbol == symbol, v


@pytest.mark.parametrize("text, kind", [
    ("Add point will be 769.20", NONE), ("Will be adding to SPY around 776", NONE),
    ("Add = 776.10", NONE), ("No adds to TSLA YET", NONE), ("Something doesn't add up", NONE),
    ("I'm not taking any big heavy add", NONE), ("Leave room to add", NONE),
    ("Added if it dips", BLOCKED), ("Adding?", BLOCKED), ("Adding up the numbers", BLOCKED),
    ("Adding when it breaks 770", BLOCKED),
])
def test_talk_about_adding(text, kind):
    assert classify(text).kind == kind


def test_a_no_ticker_add_is_an_add_into_the_channel_s_position():
    s = _parse("Added to my position\n@Julia").signals[0]
    assert (s.action.value, s.symbol, s.limit_price) == ("BUY", None, None)
    assert s.add_to_latest and s.double_up and s.contract_unspecified


def test_add_talk_is_never_read_as_an_entry():
    for text in ["This is my last add here. Thinking we get a spike soon.",
                 "I will say I like this dip on $IREN. I'm most likely going to start adding some shares here."]:
        res = _parse(text)
        assert not any(s.action.value == "BUY" and not s.add_to_latest for s in (res.signals or [])), text
