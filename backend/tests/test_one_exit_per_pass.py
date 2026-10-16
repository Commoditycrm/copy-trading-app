"""One exit per position per poller pass.

QA 2026-10-16, QQQ 720P: Trim 1 filled; the ladder's trailing stop (0.77) was
refused because the market was already below it (0.75), so the reconciler
exited 3 at market — filled. Two seconds later, in the SAME pass, the emulated
stop read the positions taken before that sale (3 still "held"), saw 0.755 below
0.77 and sold 3 again. Webull refused it ("Close intent mismatches position
direction"), but the second sell should never have gone out.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight
from app.models.user import User, UserRole
from app.services import pnl_poller

EXP = date(2026, 10, 16)


@pytest.fixture
def world(monkeypatch):
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x", connection_status="connected")
    db.add(acct)
    db.flush()
    guard = DiscordPositionGuard(user_id=user.id, symbol="QQQ", option_strike=Decimal("720"),
                                 option_right="put", option_expiry=EXP, sell_count=1,
                                 entry_price=Decimal("0.70"), stop_price=Decimal("0.77"))
    db.add(guard)
    db.commit()

    pos = SimpleNamespace(symbol="QQQ", option_strike=Decimal("720"), option_right=OptionRight.PUT,
                          option_expiry=EXP, quantity=Decimal(3), instrument_type=InstrumentType.OPTION,
                          broker_symbol="QQQ261016P00720000", current_price=Decimal("0.755"))
    sells = []

    class _Adapter:
        supports_exit_pair = False

        def get_positions(self):
            return [pos]                          # read ONCE, before anything sells

    monkeypatch.setattr("app.brokers.adapter_for", lambda a, c: _Adapter())
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda v: {})
    monkeypatch.setattr(pnl_poller, "exit_freeing_reservations",
                        lambda db, u, a, ad, p, q: sells.append(Decimal(str(q))) or SimpleNamespace())
    monkeypatch.setattr("app.services.exit_confirm.confirm_filled", lambda *a, **k: True)
    monkeypatch.setattr("app.services.live_marks.position_mark", lambda p, uid=None: Decimal("0.755"))
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    monkeypatch.setattr("app.services.discord_stop_orders.working_exit", lambda db, g: None)
    monkeypatch.setattr(pnl_poller, "_pinned", lambda *a, **k: False, raising=False)

    # The stop reconciler: Webull refuses the 0.77 stop (market 0.75), so it exits instead.
    def _refused_then_exit(db, g, held, place_stop, cancel_stop, close_position=None):
        close_position(held)
        return "closed (stop refused)"

    monkeypatch.setattr("app.services.discord_stop_orders.reconcile", _refused_then_exit)
    yield SimpleNamespace(acct=acct, sells=sells, guard_id=guard.id, user=user)

    db.rollback()
    for model, oid in ((DiscordPositionGuard, guard.id), (BrokerAccount, acct.id), (User, user.id)):
        row = db.get(model, oid)
        if row is not None:
            db.delete(row)
            db.commit()
    db.close()


def test_a_refused_stop_s_exit_is_not_followed_by_a_second_sell(world):
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, world.acct.id)
    pnl_poller._enforce_discord_ladder(acct)
    assert world.sells == [Decimal(3)], f"expected ONE sell of 3, got {world.sells}"


def test_the_next_pass_can_still_exit():
    """Only within a pass: each pass starts with an empty set and a fresh read."""
    import inspect
    src = inspect.getsource(pnl_poller._enforce_discord_ladder)
    assert src.index("exited_this_pass: set = set()") < src.index("def _close(")


def test_a_stream_fill_of_a_sell_wakes_the_ladder_pass():
    import inspect
    from app.services import webull_listener
    src = inspect.getsource(webull_listener._persist_and_fanout)
    assert "if became_filled and existing.side == OrderSide.SELL:" in src
    assert "pnl_poller.poll_now(existing.broker_account_id)" in src
