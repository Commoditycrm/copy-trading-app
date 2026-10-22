"""The Discord tab leaves out rows that name nothing that trades.

QA 2026-10-22: chatter read as a trade ("Haha I bought 2 cons" -> BUY HAHA,
"Yeah mine up 30%" -> BUY YEAH) and unreadable alerts (a "—" row) were listed
beside real alerts. An exit that names no contract is kept while its channel
has an open position — it acts on that position.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.api import discord_sources as ds
from app.database import SessionLocal
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.user import User, UserRole
from app.services import market_symbols

LISTED = {"SPY", "SKHY", "NVDA"}


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(market_symbols, "listed", lambda: frozenset(LISTED))
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    db.flush()
    chat = DiscordAlertSource(user_id=user.id, label="Chat Room", channel_id="c1", status="connected")
    alerts = DiscordAlertSource(user_id=user.id, label="Clint", channel_id="c2", status="connected")
    db.add_all([chat, alerts])
    db.flush()
    n = iter(range(1000))

    def msg(src, content, sig, status=DiscordMessageStatus.PARSED):
        m = DiscordMessage(source_id=src.id, user_id=user.id, discord_message_id=str(next(n)),
                           discord_channel_id=src.channel_id, content=content, status=status,
                           parsed_signal=sig, parsed_signals=[sig] if sig else [],
                           decision=SignalDecision.PENDING if sig else None,
                           posted_at=datetime.now(timezone.utc))
        db.add(m)
        return m

    msg(alerts, "SKHY 165C 10/30", {"action": "BUY", "asset_type": "OPTION", "symbol": "SKHY", "strike": "165"})
    msg(alerts, "SPX 7730P", {"action": "BUY", "asset_type": "OPTION", "symbol": "SPX", "strike": "7730"})
    msg(chat, "Haha I bought 2 cons", {"action": "BUY", "asset_type": "STOCK", "symbol": "HAHA"})
    msg(chat, "Yeah mine up 30%", {"action": "BUY", "asset_type": "STOCK", "symbol": "yeah"})
    msg(chat, "unreadable", None, status=DiscordMessageStatus.INVALID)
    msg(chat, "SL hit sir 50%", {"action": "SELL", "asset_type": "OPTION", "symbol": None})
    msg(alerts, "SL Hit, -35%", {"action": "SELL", "asset_type": "OPTION", "symbol": None})
    # Clint's channel holds a position; the chat room holds none.
    db.add(DiscordPositionGuard(user_id=user.id, symbol="SPY", option_strike=Decimal("770"),
                                option_right="call", option_expiry=date(2026, 10, 22), source_id=alerts.id))
    db.flush()
    yield db, user
    db.rollback()
    db.close()


def _shown(db, user):
    page = ds.list_signals(db=db, user=user, status_filter=None, search=None, limit=50, offset=0)
    return sorted(i.content for i in page.items), page.total


def test_only_rows_that_name_something_that_trades_are_listed(world):
    shown, total = _shown(*world)
    assert shown == sorted(["SKHY 165C 10/30", "SPX 7730P", "SL Hit, -35%"])
    assert total == 3, "the count agrees with what is shown"


def test_an_exit_with_no_contract_is_kept_only_where_a_position_is_open(world):
    shown, _ = _shown(*world)
    assert "SL Hit, -35%" in shown          # Clint's channel holds SPY 770C
    assert "SL hit sir 50%" not in shown    # the chat room holds nothing


def test_nothing_is_hidden_for_its_symbol_when_the_list_cannot_be_had(world, monkeypatch):
    monkeypatch.setattr(market_symbols, "listed", lambda: None)
    shown, _ = _shown(*world)
    assert "Haha I bought 2 cons" in shown and "Yeah mine up 30%" in shown
    assert "unreadable" not in shown        # no symbol at all is still left out


def test_a_status_filter_still_shows_everything_it_matches(world):
    db, user = world
    page = ds.list_signals(db=db, user=user, status_filter="parsed", search=None, limit=50, offset=0)
    assert "Haha I bought 2 cons" in [i.content for i in page.items]


def test_index_roots_count_as_tradable(monkeypatch):
    monkeypatch.setattr(market_symbols, "listed", lambda: frozenset())
    assert market_symbols.is_listed("SPX") is True and market_symbols.is_listed("$XSP") is True
    assert market_symbols.is_listed("HAHA") is False


class _NoRedis:
    def get(self, k):
        return None

    def set(self, *a, **k):
        pass


def test_the_list_is_fetched_once_and_a_failure_is_not_retried_every_load(monkeypatch):
    calls = []
    monkeypatch.setattr(market_symbols, "_redis", lambda: _NoRedis())
    monkeypatch.setattr(market_symbols, "_cache", None)
    monkeypatch.setattr(market_symbols, "_failed_at", None)
    monkeypatch.setattr(market_symbols, "_fetch", lambda: calls.append(1) or None)
    assert market_symbols.listed() is None and market_symbols.listed() is None
    assert len(calls) == 1, "a failed fetch waits before trying again"

    monkeypatch.setattr(market_symbols, "_failed_at", None)
    monkeypatch.setattr(market_symbols, "_fetch", lambda: calls.append(2) or frozenset({"SPY"}))
    assert market_symbols.listed() == {"SPY"} and market_symbols.listed() == {"SPY"}
    assert calls == [1, 2]
