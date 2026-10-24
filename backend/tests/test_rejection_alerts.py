"""An order the broker rejects AFTER accepting it is reported, and texted.

A rejection at placement was always reported where it happened. One that came
later — the order accepted, then rejected, seen through the order stream, the
poll or the fill sync — only changed the order's status: no notice, no text.
"""
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import notifications, rejection_alerts


@pytest.fixture
def world(monkeypatch):
    rejection_alerts.install()
    sent = []
    monkeypatch.setattr(rejection_alerts, "notify", lambda ids: sent.extend(ids))
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    db.flush()
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x")
    db.add(acct)
    db.flush()

    def order(status):
        o = Order(id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
                  instrument_type=InstrumentType.STOCK, symbol="SPY", side=OrderSide.SELL,
                  order_type=OrderType.LIMIT, quantity=Decimal(1), limit_price=Decimal("1"),
                  status=status)
        db.add(o)
        db.commit()
        return o

    yield SimpleNamespace(db=db, order=order, sent=sent)
    db.rollback()
    for o in db.query(Order).filter(Order.user_id == user.id).all():
        db.delete(o)
    db.delete(db.get(BrokerAccount, acct.id))
    db.delete(db.get(User, user.id))
    db.commit()
    db.close()


@pytest.mark.parametrize("before", [OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED])
def test_a_rejection_after_the_broker_accepted_is_reported(world, before):
    o = world.order(before)
    o.status = OrderStatus.REJECTED
    world.db.commit()
    assert world.sent == [o.id]


def test_a_rejection_at_placement_is_not_reported_twice(world):
    """PENDING -> REJECTED is the placement path, which reports it itself."""
    o = world.order(OrderStatus.PENDING)
    o.status = OrderStatus.REJECTED
    world.db.commit()
    assert world.sent == []


def test_a_rolled_back_rejection_is_not_reported(world):
    o = world.order(OrderStatus.SUBMITTED)
    o.status = OrderStatus.REJECTED
    world.db.flush()
    world.db.rollback()
    assert world.sent == []


def test_other_changes_are_not_reported(world):
    o = world.order(OrderStatus.SUBMITTED)
    o.status = OrderStatus.FILLED
    world.db.commit()
    assert world.sent == []


def test_the_message_names_the_order_and_the_reason():
    o = SimpleNamespace(side=OrderSide.SELL, reject_reason="insufficient quantity", parent_order_id=None,
                        instrument_type=InstrumentType.STOCK, symbol="SPY", option_right=None,
                        option_strike=None, option_expiry=None)
    kind, msg = rejection_alerts.message_for(o)
    assert kind == "order.rejected" and "SELL" in msg and "insufficient quantity" in msg
    o.parent_order_id = uuid.uuid4()
    assert rejection_alerts.message_for(o)[0] == "copy.rejected"


def test_both_kinds_are_text_message_categories():
    """Texts go out only for registered categories; these two are, under
    "Trade rejected"."""
    assert notifications._sms_pref_attr("order.rejected") == "sms_on_trade_rejected"
    assert notifications._sms_pref_attr("copy.rejected") == "sms_on_trade_rejected"


def test_the_notice_is_texted_to_the_phone_in_settings(monkeypatch):
    """End to end: the notice is created for the order's owner, and texted to
    the phone in Settings when texts and "Trade rejected" are on."""
    import threading

    texts = []
    monkeypatch.setattr("app.services.sms.send_sms", lambda to, body: texts.append((to, body)) or True)

    class _Now(threading.Thread):                      # run the send at once, not off-thread
        def start(self):
            self.run()

    monkeypatch.setattr(notifications.threading, "Thread", _Now)
    monkeypatch.setattr(notifications.events, "publish", lambda *a, **k: None)
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local", password_hash="x",
                role=UserRole.TRADER, phone="+15551234567", sms_notifications_enabled=True,
                sms_on_trade_rejected=True)
    db.add(user)
    db.flush()
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x")
    db.add(acct)
    db.flush()
    o = Order(id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
              instrument_type=InstrumentType.STOCK, symbol="SPY", side=OrderSide.SELL,
              order_type=OrderType.LIMIT, quantity=Decimal(1), limit_price=Decimal("1"),
              status=OrderStatus.REJECTED, reject_reason="This order cannot be entered")
    db.add(o)
    db.commit()
    try:
        rejection_alerts.notify([o.id])
        assert len(texts) == 1 and texts[0][0] == "+15551234567"
        assert "rejected" in texts[0][1] and "cannot be entered" in texts[0][1]
    finally:
        from app.models.notification import Notification

        db.query(Notification).filter(Notification.user_id == user.id).delete()
        db.delete(db.get(Order, o.id))
        db.delete(db.get(BrokerAccount, acct.id))
        db.delete(db.get(User, user.id))
        db.commit()
        db.close()
