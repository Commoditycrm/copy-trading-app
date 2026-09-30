"""A Discord trader's orders are not copied to subscribers.

Subscribers of a Discord trader trade the channel ALERTS on their own settings
(services/discord_subscribers.py), independent of the trader's order. So every
order the Discord machinery places for the trader — the alert itself, ladder
stops, trailing / AI exits — skips the order fanout; copying them would trade
subscribers twice.

Also pinned: work handed to _InlineTasks off the request path (auto-trim,
pasted Self alerts) used to build an async coroutine and never run it.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import discord_sources
from app.services import pnl_poller


def test_the_alert_order_skips_the_fanout():
    src = inspect.getsource(discord_sources._execute_signal)
    call = src[src.index("order = _place_trader_order("):]
    call = call[:call.index(")\n    except HTTPException")]
    assert "skip_fanout=True" in call


def test_ladder_and_ai_exits_skip_the_fanout():
    assert "skip_fanout=True" in inspect.getsource(pnl_poller.place_exit)


def test_ladder_stops_skip_the_fanout():
    assert "skip_fanout=True" in inspect.getsource(pnl_poller._make_stop_placer)


def test_inline_tasks_run_async_work():
    ran = []

    async def _fanout(x):
        ran.append(x)

    discord_sources._InlineTasks().add_task(_fanout, "order-1")
    assert ran == ["order-1"]


def test_inline_tasks_still_run_sync_work():
    ran = []
    discord_sources._InlineTasks().add_task(ran.append, "x")
    assert ran == ["x"]
