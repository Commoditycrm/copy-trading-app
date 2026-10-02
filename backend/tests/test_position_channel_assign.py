"""POST /api/positions/channel — assign a held position to a channel by hand.

More than a label: the assigned channel's exit settings then manage the
position (discord_channel_settings.for_guard reads the assignment first).
"""
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.discord_sources as ds
import app.api.positions as positions
import app.services.discord_position_guard as guards
from app.models.order import OptionRight
from app.schemas.position import PositionChannelIn
from app.services import discord_channel_settings as dcs

USER = SimpleNamespace(id=uuid.uuid4())
CLINT = SimpleNamespace(id=uuid.uuid4(), user_id=USER.id, label="Clint", channel_name="clint-alerts")
SELF = SimpleNamespace(id=uuid.uuid4(), user_id=USER.id, label="Self", channel_name="Self")


class _DB:
    def __init__(self, source=CLINT):
        self.source, self.commits = source, 0

    def get(self, model, key):
        return self.source if self.source is not None and key == self.source.id else None

    def commit(self):
        self.commits += 1


@pytest.fixture
def wired(monkeypatch):
    state = SimpleNamespace(guard=None, created=[])
    monkeypatch.setattr(ds, "require_discord_member", lambda **kw: kw["user"])
    monkeypatch.setattr(ds, "_self_source", lambda db, user: SELF)
    monkeypatch.setattr(guards, "find", lambda *a, **k: state.guard)

    def _on_buy(db, user_id, symbol, strike, right, expiry, entry_price=None, **kw):
        state.guard = SimpleNamespace(source_id=None, entry_price=entry_price, symbol=symbol)
        state.created.append((symbol, strike, right, expiry, entry_price))
        return state.guard

    monkeypatch.setattr(guards, "on_buy", _on_buy)
    return state


def _body(channel, **kw):
    base = dict(symbol="spy", option_strike=Decimal("764"), option_right=OptionRight.CALL,
                option_expiry="2026-10-02", channel=channel, entry_price=Decimal("1.02"))
    base.update(kw)
    return PositionChannelIn(**base)


def test_assigning_a_channel_records_it_on_the_positions_ladder(wired):
    wired.guard = SimpleNamespace(source_id=None)
    db = _DB()
    out = positions.assign_position_channel(_body(str(CLINT.id)), db=db, user=USER)
    assert wired.guard.source_id == CLINT.id and db.commits == 1
    assert out == {"channel": str(CLINT.id), "discord_channel": "Clint"}
    assert wired.created == []                      # the ladder it had is kept as it is


def test_a_hand_opened_position_gets_a_ladder_from_its_cost(wired):
    out = positions.assign_position_channel(_body("self"), db=_DB(), user=USER)
    assert wired.created == [("SPY", Decimal("764"), OptionRight.CALL,
                              _body("self").option_expiry, Decimal("1.02"))]
    assert wired.guard.source_id == SELF.id and out["discord_channel"] == "Self"


def test_auto_goes_back_to_the_opening_channel(wired):
    wired.guard = SimpleNamespace(source_id=CLINT.id)
    db = _DB()
    out = positions.assign_position_channel(_body("auto"), db=db, user=USER)
    assert wired.guard.source_id is None and db.commits == 1
    assert out == {"channel": "auto", "discord_channel": None}


def test_auto_on_a_position_with_no_ladder_creates_nothing(wired):
    db = _DB()
    positions.assign_position_channel(_body("auto"), db=db, user=USER)
    assert wired.created == [] and db.commits == 0


def test_a_stock_position_is_matched_without_contract_fields(wired):
    positions.assign_position_channel(
        _body("self", symbol="AAPL", option_strike=None, option_right=None, option_expiry=None),
        db=_DB(), user=USER)
    assert wired.created[0][:4] == ("AAPL", None, None, None)


@pytest.mark.parametrize("channel, source", [
    (str(uuid.uuid4()), CLINT),                                             # unknown id
    ("not-a-channel", CLINT),                                               # not an id at all
    (str(CLINT.id), SimpleNamespace(id=CLINT.id, user_id=uuid.uuid4())),    # someone else's channel
])
def test_only_the_users_own_channels(wired, channel, source):
    with pytest.raises(HTTPException) as exc:
        positions.assign_position_channel(_body(channel), db=_DB(source), user=USER)
    assert exc.value.status_code == 404 and wired.created == []


def test_the_assigned_channels_settings_manage_the_position(monkeypatch):
    """for_guard reads the assignment before the opening order's channel."""
    seen = []
    monkeypatch.setattr(dcs, "effective", lambda db, user_id, source_id: seen.append(source_id) or "settings")
    monkeypatch.setattr(dcs, "source_for_order", lambda db, order_id: pytest.fail("the assignment wins"))
    guard = SimpleNamespace(source_id=CLINT.id, entry_order_id=uuid.uuid4())
    assert dcs.for_guard(None, USER.id, guard) == "settings" and seen == [CLINT.id]
