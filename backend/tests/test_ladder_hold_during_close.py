"""A manual close holds the Discord ladder off its position until it is sent.

QA 2026-10-22, CSCO 103P: the close released the take-profit and its linked
stop, paused for the cancels to settle, and in that gap the ladder's own pass
put the take-profit and stop straight back on all 3 contracts. Webull refused
the close ("will reverse an existing position … cancel an open order").
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app.services.discord_take_profit as tp
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight
from app.models.user import User, UserRole
from app.services import ladder_hold, pnl_poller


class _Redis:
    def __init__(self):
        self.keys = {}

    def set(self, k, v, ex=None):
        self.keys[k] = v

    def delete(self, k):
        self.keys.pop(k, None)

    def exists(self, k):
        return int(k in self.keys)


@pytest.fixture
def r(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(ladder_hold, "_redis", lambda: fake)
    return fake


def test_a_hold_is_seen_and_cleared(r):
    gid = uuid.uuid4()
    assert ladder_hold.held(gid) is False
    assert ladder_hold.hold(gid) is True and ladder_hold.held(gid) is True
    ladder_hold.clear(gid)
    assert ladder_hold.held(gid) is False


def test_a_request_clears_its_holds_when_it_ends_even_on_a_refusal(r):
    gid = uuid.uuid4()

    @ladder_hold.released_after
    def close():
        ladder_hold.hold_for_request(gid)
        assert ladder_hold.held(gid)
        raise RuntimeError("OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION")

    with pytest.raises(RuntimeError):
        close()
    assert ladder_hold.held(gid) is False, "a refused close puts protection straight back"


def test_no_redis_means_no_hold(monkeypatch):
    def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(ladder_hold, "_redis", _down)
    assert ladder_hold.hold(uuid.uuid4()) is False and ladder_hold.held(uuid.uuid4()) is False


def test_the_close_route_keeps_its_signature_for_fastapi():
    import inspect

    from app.api import positions

    params = inspect.signature(positions.close_position).parameters
    assert {"broker_symbol", "payload", "broker_account_id", "db", "user"} <= set(params)


# ── the ladder pass skips a held position ────────────────────────────────────

EXP = date(2026, 10, 23)


@pytest.fixture
def world(monkeypatch, r):
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x", connection_status="connected")
    db.add(acct)
    db.flush()
    guard = DiscordPositionGuard(user_id=user.id, symbol="CSCO", option_strike=Decimal("103"),
                                 option_right="put", option_expiry=EXP, sell_count=0,
                                 entry_price=Decimal("0.62"), stop_price=Decimal("0.12"))
    db.add(guard)
    db.commit()
    pos = SimpleNamespace(symbol="CSCO", option_strike=Decimal("103"), option_right=OptionRight.PUT,
                          option_expiry=EXP, quantity=Decimal(3), instrument_type=InstrumentType.OPTION,
                          broker_symbol="CSCO261023P00103000", current_price=Decimal("0.40"))

    class _Adapter:
        supports_exit_pair = True

        def get_positions(self):
            return [pos]

    reconciled = []
    monkeypatch.setattr("app.brokers.adapter_for", lambda a, c: _Adapter())
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda v: {})
    monkeypatch.setattr("app.services.discord_stop_orders.reconcile",
                        lambda db, g, held, **k: reconciled.append(g.symbol) or "in sync")
    monkeypatch.setattr("app.services.fills_sync.repair_unrecorded_fills", lambda *a, **k: 0)
    monkeypatch.setattr("app.services.discord_trailing_stop.enforce", lambda *a, **k: 0)
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    monkeypatch.setattr(pnl_poller, "_pinned", lambda *a, **k: False, raising=False)
    yield SimpleNamespace(acct=acct, guard_id=guard.id, reconciled=reconciled)
    db.rollback()
    for model, oid in ((DiscordPositionGuard, guard.id), (BrokerAccount, acct.id), (User, user.id)):
        row = db.get(model, oid)
        if row is not None:
            db.delete(row)
            db.commit()
    db.close()


def _pass(world):
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, world.acct.id)
    pnl_poller._enforce_discord_ladder(acct)


def test_a_held_position_is_left_alone_by_the_ladder(world):
    ladder_hold.hold(world.guard_id)
    _pass(world)
    assert world.reconciled == [], "nothing is put back while the close is being sent"


def test_once_the_hold_is_gone_the_ladder_carries_on(world):
    _pass(world)
    assert world.reconciled == ["CSCO"]


# ── the take-profit waits for a working exit, as the stop does ───────────────

def test_the_take_profit_is_not_placed_while_an_exit_is_working():
    from test_discord_take_profit import _Book, _guard, _ts

    book, g = _Book(), _guard(stop_price=None)
    for outcome in ("waiting (exit working)", "exit already working"):
        out = tp.reconcile(book, g, Decimal(3), _ts(), Decimal("0.40"),
                           place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                           reconcile_stop=lambda o=outcome: o)
        assert "being closed" in out and g.tp_qty is None
    assert book.calls == []
