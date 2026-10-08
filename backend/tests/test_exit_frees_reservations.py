"""A trailing exit / stop-out frees the contracts resting orders hold, first.

QA 2026-10-08, SPY 775P: a 10% trailing stop armed after T1 fired three times
and Webull rejected every market sell — T2's take-profit pair and the ladder
stop were holding the contracts. A manual close always released them first.
"""
from types import SimpleNamespace

from app.services import discord_stop_orders, pnl_poller


def test_the_resting_orders_are_released_before_the_exit_is_placed(monkeypatch):
    calls = []
    monkeypatch.setattr(discord_stop_orders, "release_for_position",
                        lambda db, user, pos: calls.append("release") or True)
    monkeypatch.setattr(pnl_poller, "place_exit",
                        lambda db, trader, acct, adapter, pos, qty: calls.append(("exit", qty)) or "order")
    pos = SimpleNamespace(symbol="SPY")
    assert pnl_poller.exit_freeing_reservations(None, SimpleNamespace(), None, None, pos, 5) == "order"
    assert calls == ["release", ("exit", 5)]


def test_a_failed_release_still_tries_the_exit(monkeypatch):
    calls = []

    def _boom(*a):
        raise RuntimeError("broker down")

    monkeypatch.setattr(discord_stop_orders, "release_for_position", _boom)
    monkeypatch.setattr(pnl_poller, "place_exit", lambda *a: calls.append("exit") or "order")
    pnl_poller.exit_freeing_reservations(None, SimpleNamespace(), None, None, SimpleNamespace(symbol="SPY"), 5)
    assert calls == ["exit"]


def test_the_poller_s_exits_go_through_it():
    import inspect

    src = inspect.getsource(pnl_poller._enforce_discord_trailing_stops)
    assert "exit_freeing_reservations(db, db.get(User, acct.user_id), live_acct, adapter, pos, quantity)" in src
