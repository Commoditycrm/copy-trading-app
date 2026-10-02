"""The position row's stop controls reach the Discord ladder's stop.

The ladder's stop is its own order, re-placed by the stop reconciler for as
long as the ladder holds a level. Cancelling only the order (which is all
"Cancel all open orders" could do) brought it straight back. The row's menu
now clears the level too, and the positions payload says which rows have one.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import positions as api
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight

EXP = date(2026, 10, 16)
USER = uuid.uuid4()


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    with Session(eng) as s:
        yield s


def _guard(db, stop="1.50", strike="100", closed=False):
    from datetime import datetime, timezone

    g = DiscordPositionGuard(
        user_id=USER, symbol="SPY", option_strike=Decimal(strike),
        option_right=OptionRight.PUT.value, option_expiry=EXP, sell_count=1,
        entry_price=Decimal("2.00"), stop_price=Decimal(stop) if stop else None,
        closed_at=datetime.now(timezone.utc) if closed else None,
    )
    db.add(g); db.flush()
    return g


def _pos(strike="100"):
    return SimpleNamespace(
        symbol="SPY", option_strike=Decimal(strike), option_right=OptionRight.PUT,
        option_expiry=EXP, instrument_type=InstrumentType.OPTION, quantity=Decimal(3),
        broker_symbol=f"SPY261016P00{int(Decimal(strike)):03d}000",
    )


def test_each_row_reports_its_live_ladder_stop(db):
    _guard(db, stop="1.50", strike="100")
    _guard(db, stop="9.99", strike="101", closed=True)     # a finished ladder
    rows = [_pos("100"), _pos("101"), _pos("102")]
    api._attach_ladder_stops(db, USER, rows)
    assert [r.ladder_stop_price for r in rows] == [Decimal("1.50"), None, None]


def _call_cancel(db, monkeypatch, resting=()):
    import app.api.discord_sources as ds
    from app.services import audit

    monkeypatch.setattr(api, "adapter_for", lambda acct, creds: SimpleNamespace(get_positions=lambda: [_pos()]))
    monkeypatch.setattr(api, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(api, "_resting_stop_orders", lambda db, uid, pos: list(resting))
    cancelled = []
    monkeypatch.setattr(ds, "_cancel_stop_order", lambda db, user: cancelled.append)
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    real_get = db.get
    db.get = lambda model, pk: (SimpleNamespace(user_id=USER, encrypted_credentials=b"")
                                if model is api.BrokerAccount else real_get(model, pk))
    out = api.cancel_position_stops(
        _pos().broker_symbol, SimpleNamespace(headers={}, client=None),
        broker_account_id=uuid.uuid4(), db=db, user=SimpleNamespace(id=USER),
    )
    return out, cancelled


def test_cancel_clears_the_level_as_well_as_the_order(db, monkeypatch):
    g = _guard(db)
    oid = uuid.uuid4()
    g.stop_order_id = oid
    out, cancelled = _call_cancel(db, monkeypatch)
    assert cancelled == [oid]                             # the resting order is pulled
    assert g.stop_price is None and g.stop_order_id is None   # and won't be re-placed
    assert out["removed"] == ["stop @ 1.50"]


def test_cancel_also_disarms_a_trailing_exit(db, monkeypatch):
    g = _guard(db, stop=None)
    g.trail_qty, g.trail_amount, g.peak_price = Decimal(3), Decimal("0.25"), Decimal("2.40")
    out, _ = _call_cancel(db, monkeypatch)
    assert g.trail_qty is None and g.trail_amount is None
    assert out["removed"] == ["trailing exit on 3"]


def test_cancel_pulls_other_resting_stop_orders(db, monkeypatch):
    """e.g. a native trailing stop on a stock, which has no ladder level."""
    from app.models.order import OrderType

    native = SimpleNamespace(id=uuid.uuid4(), order_type=OrderType.TRAILING_STOP)
    out, cancelled = _call_cancel(db, monkeypatch, resting=[native])
    assert cancelled == [native.id]
    assert out["removed"] == ["trailing_stop order"]


def test_no_stops_is_a_404(db, monkeypatch):
    _guard(db, stop=None)
    with pytest.raises(api.HTTPException) as exc:
        _call_cancel(db, monkeypatch)
    assert exc.value.status_code == 404


# ── Stop at a P&L level (the expanded row's Stop button) ────────────────────

def _call_set_stop(db, monkeypatch, pct, mark="2.40", avg="2.00"):
    from app.services import audit

    pos = _pos()
    pos.current_price = Decimal(mark)
    pos.avg_entry_price = Decimal(avg)
    monkeypatch.setattr(api, "adapter_for", lambda acct, creds: SimpleNamespace(get_positions=lambda: [pos]))
    monkeypatch.setattr(api, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    real_get = db.get
    db.get = lambda model, pk: (SimpleNamespace(user_id=USER, encrypted_credentials=b"")
                                if model is api.BrokerAccount else real_get(model, pk))
    return api.set_position_stop(
        pos.broker_symbol, SimpleNamespace(headers={}, client=None),
        broker_account_id=uuid.uuid4(), pnl_pct=Decimal(pct), db=db, user=SimpleNamespace(id=USER),
    )


def test_stop_levels_are_measured_from_the_average_price(db, monkeypatch):
    g = _guard(db, stop=None)                        # ladder entry 2.00, average 2.00
    out = _call_set_stop(db, monkeypatch, "-25")
    assert Decimal(out["stop_price"]) == Decimal("1.50")
    assert g.stop_price == Decimal("1.50")           # the reconciler places it


def test_a_break_even_stop_uses_the_average_not_the_opening_price(db, monkeypatch):
    """Opened at 2.00, added lower: the position now averages 1.60, which is what
    the row shows and what 0% was previewed against. The ladder still remembers
    2.00 — a stop there is not break-even (and above a 1.90 market it is refused)."""
    g = _guard(db, stop=None)                        # ladder entry 2.00
    out = _call_set_stop(db, monkeypatch, "0", avg="1.60", mark="1.90")
    assert Decimal(out["stop_price"]) == Decimal("1.60") == g.stop_price
    assert Decimal(out["entry_price"]) == Decimal("1.60")
    assert g.entry_price == Decimal("2.00")          # the ladder's own reference is untouched


def test_the_ladders_entry_is_the_fallback_when_the_broker_gives_no_average(db, monkeypatch):
    g = _guard(db, stop=None)                        # ladder entry 2.00
    out = _call_set_stop(db, monkeypatch, "0", avg="0", mark="2.40")
    assert Decimal(out["stop_price"]) == Decimal("2.00") == g.stop_price


def test_break_even_and_profit_levels_are_allowed_below_the_market(db, monkeypatch):
    g = _guard(db, stop=None)
    _call_set_stop(db, monkeypatch, "0", mark="2.40")
    assert g.stop_price == Decimal("2.00")
    _call_set_stop(db, monkeypatch, "10", mark="2.40")
    assert g.stop_price == Decimal("2.20")


def test_a_stop_at_or_above_the_market_is_refused(db, monkeypatch):
    """Alpaca refuses it, and the refusal would trigger the sell-the-rest fallback."""
    g = _guard(db, stop=None)
    with pytest.raises(api.HTTPException) as exc:
        _call_set_stop(db, monkeypatch, "25", mark="2.40")    # 2.50 >= 2.40
    assert exc.value.status_code == 422
    assert g.stop_price is None


def test_a_position_without_a_ladder_gets_one(db, monkeypatch):
    out = _call_set_stop(db, monkeypatch, "-10", avg="3.00", mark="3.20")
    assert Decimal(out["stop_price"]) == Decimal("2.70")
    from sqlalchemy import select
    g = db.execute(select(DiscordPositionGuard)).scalars().one()
    assert g.stop_price == Decimal("2.70") and g.entry_price == Decimal("3.00")
