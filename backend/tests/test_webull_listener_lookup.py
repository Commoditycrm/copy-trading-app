"""The listener has to recognise orders THIS app placed.

Webull gives one order two identifiers. WebullAdapter.place_order stores OUR
client_order_id in broker_order_id -- it is the handle every later cancel,
replace and read uses -- while the order feed keys on Webull's own order_id.

Matching only the feed's id meant the lookup always missed for our own orders:
the contract filled at the broker and our row stayed SUBMITTED, with no event
to push the fill to the UI. The order history only looked right after a manual
refresh, and even then only once something else happened to correct it.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.order import (
    InstrumentType, Order, OrderSide, OrderStatus, OrderType,
)
from app.services.webull_listener import find_placed_order

USER = uuid.uuid4()
OUR_COID = uuid.uuid4().hex          # what we store, dashes stripped
WEBULL_OID = "C4I05SUKUQNF6QUMEKR1BON2B9"


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)
    with Session(eng) as s:
        s.add(Order(
            id=uuid.uuid4(), user_id=USER, broker_order_id=OUR_COID,
            symbol="SPY", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            instrument_type=InstrumentType.OPTION, quantity=Decimal(4),
            status=OrderStatus.SUBMITTED, created_at=datetime.now(timezone.utc),
        ))
        s.commit()
        yield s


def test_our_own_order_is_found_by_the_client_order_id(db):
    """The feed reports Webull's order_id, which we never stored. Without also
    matching the client_order_id the row is never updated and the fill is
    invisible until something else corrects it."""
    found = find_placed_order(db, USER, WEBULL_OID, OUR_COID)
    assert found is not None
    assert found.broker_order_id == OUR_COID


def test_an_order_stored_under_the_broker_id_is_still_found(db):
    """Externally-placed orders ARE stored under Webull's id -- that path must
    keep working."""
    db.add(Order(
        id=uuid.uuid4(), user_id=USER, broker_order_id=WEBULL_OID,
        symbol="QQQ", side=OrderSide.BUY, order_type=OrderType.MARKET,
        instrument_type=InstrumentType.STOCK, quantity=Decimal(1),
        status=OrderStatus.SUBMITTED, created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    found = find_placed_order(db, USER, WEBULL_OID, "")
    assert found is not None and found.symbol == "QQQ"


def test_another_traders_order_is_not_returned(db):
    assert find_placed_order(db, uuid.uuid4(), WEBULL_OID, OUR_COID) is None


def test_no_identifiers_matches_nothing(db):
    """An empty id must not degenerate into a query that matches any row."""
    assert find_placed_order(db, USER, "", "") is None


def test_an_unknown_order_is_not_matched(db):
    assert find_placed_order(db, USER, "SOME_OTHER_OID", uuid.uuid4().hex) is None


def test_the_dashed_client_id_from_the_poll_still_matches(db):
    """Webull caps client_order_id at 32 chars, so we send the UUID with dashes
    STRIPPED and store that. The day-orders endpoint echoes it back in canonical
    DASHED form while order-history returns the stripped one. A plain string
    compare missed on the poll path -- and once the app-originated marker
    expired (120s) the listener inserted the order a second time, rebuilt from
    the feed as a STOCK with no strike, which also breaks realized P&L by 100x."""
    dashed = str(uuid.UUID(hex=OUR_COID))
    assert "-" in dashed
    found = find_placed_order(db, USER, WEBULL_OID, dashed)
    assert found is not None
    assert found.broker_order_id == OUR_COID


def test_a_dashed_id_that_belongs_to_nobody_still_matches_nothing(db):
    assert find_placed_order(db, USER, "", str(uuid.uuid4())) is None
