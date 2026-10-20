"""The On Fill stop is set before the first take-profit is planned.

QA 2026-10-19, QQQ 722P (Srini, Webull): the entry filled 10 @ 0.97 and the
ladder pass placed Trim 1 (8 @ 1.22) at 16:00:57.59 — 0.1s BEFORE the auto-trim
sweep set the On Fill stop at 0.48. With no stop level, Trim 1 went out as a
plain limit with no linked stop, and the stop on the other 2 was not placed
either. The next pass came after the price had already run to the target, so
the position never had a stop at the broker.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_ladder, discord_take_profit, pnl_poller

EXP = date(2026, 10, 19)


@pytest.fixture
def world(monkeypatch):
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.SUBSCRIBER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x", connection_status="connected")
    db.add(acct)
    db.flush()
    entry = Order(id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
                  instrument_type=InstrumentType.OPTION, symbol="QQQ", option_strike=D("722"),
                  option_right=OptionRight.PUT, option_expiry=EXP, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=D(10), status=OrderStatus.FILLED,
                  filled_quantity=D(10), filled_avg_price=D("0.97"), broker_order_id=uuid.uuid4().hex,
                  closed_at=datetime.now(timezone.utc))
    db.add(entry)
    db.flush()
    guard = DiscordPositionGuard(user_id=user.id, symbol="QQQ", option_strike=D("722"),
                                 option_right="put", option_expiry=EXP, sell_count=0,
                                 entry_price=D("0.97"), entry_order_id=entry.id, fill_stop_done=False)
    db.add(guard)
    db.commit()

    pos = SimpleNamespace(symbol="QQQ", option_strike=D("722"), option_right=OptionRight.PUT,
                          option_expiry=EXP, quantity=D(10), instrument_type=InstrumentType.OPTION,
                          broker_symbol="QQQ261019P00722000", current_price=D("0.97"))

    class _Adapter:
        supports_exit_pair = True

        def get_positions(self):
            return [pos]

    seen = []

    def _reconcile(db, g, held, ts, mark, **kw):
        seen.append(g.stop_price)               # the level the take-profit is planned with
        return "placed"

    monkeypatch.setattr("app.brokers.adapter_for", lambda a, c: _Adapter())
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda v: {})
    monkeypatch.setattr(discord_take_profit, "possible", lambda *a: True)
    monkeypatch.setattr(discord_take_profit, "enabled", lambda ts: True)
    monkeypatch.setattr(discord_take_profit, "active", lambda ts, ad: True)
    monkeypatch.setattr(discord_take_profit, "reconcile", _reconcile)
    monkeypatch.setattr(discord_ladder, "fill_stop_pct", lambda ts: D("-50"))
    monkeypatch.setattr(discord_ladder, "fill_stop_trails", lambda ts: False)
    monkeypatch.setattr("app.services.live_marks.position_mark", lambda p, uid=None: D("0.97"))
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    monkeypatch.setattr("app.services.fills_sync.repair_unrecorded_fills", lambda *a, **k: 0)
    monkeypatch.setattr("app.services.discord_trailing_stop.enforce", lambda *a, **k: 0)
    monkeypatch.setattr(pnl_poller, "_pinned", lambda *a, **k: False, raising=False)
    yield SimpleNamespace(acct=acct, guard_id=guard.id, seen=seen)

    db.rollback()
    for model, oid in ((DiscordPositionGuard, guard.id), (Order, entry.id),
                       (BrokerAccount, acct.id), (User, user.id)):
        row = db.get(model, oid)
        if row is not None:
            db.delete(row)
            db.commit()
    db.close()


def test_the_first_take_profit_is_planned_with_the_on_fill_stop_already_set(world):
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, world.acct.id)
    pnl_poller._enforce_discord_ladder(acct)
    assert world.seen == [D("0.48")], (
        "Trim 1 must be planned with the On Fill stop, so it rests with a linked stop "
        "and the rest of the position gets its stop in the same pass")
    with SessionLocal() as db:
        g = db.get(DiscordPositionGuard, world.guard_id)
        assert g.fill_stop_done is True and g.stop_price == D("0.48")
