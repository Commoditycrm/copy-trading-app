"""Who gets the Sell-All / Snapshot / Re-entry suite.

A Discord trader's subscribers trade that trader's channels on their own
settings, and the trader cannot act on their positions — the subscriber-
targeting bulk actions refuse, and the bar hides them. So exiting has to be
theirs to do, without waiting on an admin toggle.

Everyone else is unchanged: the admin allow-list still decides.
"""
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.deps import sell_all_allowed
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, SubscriberSettings):
        m.__table__.create(eng)
    return sessionmaker(bind=eng)()


def _user(db, role, *, sell_all=False, discord=False, active=True):
    u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@x.com", password_hash="x",
             role=role, is_active=active, sell_all_access=sell_all,
             discord_enabled=discord)
    db.add(u); db.commit()
    return u


def _follows(db, sub, trader):
    db.add(SubscriberSettings(user_id=sub.id, following_trader_id=trader.id))
    db.commit()


def test_a_discord_traders_subscriber_gets_it_without_the_admin_flag(db):
    trader = _user(db, UserRole.TRADER, discord=True)
    sub = _user(db, UserRole.SUBSCRIBER)          # flag OFF
    _follows(db, sub, trader)
    assert sell_all_allowed(db, sub) is True


def test_a_non_discord_traders_subscriber_still_needs_the_flag(db):
    trader = _user(db, UserRole.TRADER, discord=False)
    sub = _user(db, UserRole.SUBSCRIBER)
    _follows(db, sub, trader)
    assert sell_all_allowed(db, sub) is False


def test_a_subscriber_following_nobody_still_needs_the_flag(db):
    assert sell_all_allowed(db, _user(db, UserRole.SUBSCRIBER)) is False


def test_an_inactive_discord_trader_grants_nothing(db):
    trader = _user(db, UserRole.TRADER, discord=True, active=False)
    sub = _user(db, UserRole.SUBSCRIBER)
    _follows(db, sub, trader)
    assert sell_all_allowed(db, sub) is False


def test_the_admin_flag_still_works_on_its_own(db):
    assert sell_all_allowed(db, _user(db, UserRole.SUBSCRIBER, sell_all=True)) is True
    assert sell_all_allowed(db, _user(db, UserRole.TRADER, sell_all=True)) is True


def test_a_trader_without_the_flag_is_unchanged(db):
    """Discord access does NOT grant a trader the suite — only the allow-list."""
    assert sell_all_allowed(db, _user(db, UserRole.TRADER, discord=True)) is False


def test_an_admin_never_gets_it(db):
    assert sell_all_allowed(db, _user(db, UserRole.ADMIN, sell_all=True)) is False


def test_granting_access_never_writes_the_admins_flag(db):
    """/me sets a DERIVED field. Writing to sell_all_access instead would mark
    the row dirty and could persist a grant the admin never made."""
    trader = _user(db, UserRole.TRADER, discord=True)
    sub = _user(db, UserRole.SUBSCRIBER)
    _follows(db, sub, trader)
    assert sell_all_allowed(db, sub) is True
    db.commit()
    db.refresh(sub)
    assert sub.sell_all_access is False, "the stored admin flag was modified"
