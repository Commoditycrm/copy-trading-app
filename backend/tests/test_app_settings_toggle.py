"""Admin runtime flag store (app_settings) — the market-stream toggle backend.

A DB override beats the env default; set_flag writes through and busts the cache
so a supervisor's next _enabled() poll sees it; a missing/unknown value falls
back to the default.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.services import app_settings


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    AppSetting.__table__.create(eng)
    return Session(eng)


def _patch_sessionlocal(monkeypatch, db):
    # app_settings.flag opens its own SessionLocal — point it at our in-memory db.
    monkeypatch.setattr(app_settings, "SessionLocal", lambda: db)
    app_settings._cache.clear()


def test_default_used_when_no_override(monkeypatch):
    db = _session()
    _patch_sessionlocal(monkeypatch, db)
    assert app_settings.flag("alpaca_market_stream_enabled", default=False) is False
    assert app_settings.flag("alpaca_market_stream_enabled", default=True) is True


def test_override_beats_default(monkeypatch):
    db = _session()
    _patch_sessionlocal(monkeypatch, db)
    # Admin turns it ON though env default is OFF.
    app_settings.set_flag(db, "alpaca_market_stream_enabled", True)
    db.commit()
    assert app_settings.flag("alpaca_market_stream_enabled", default=False) is True
    # ...and OFF though env default is ON.
    app_settings.set_flag(db, "alpaca_market_stream_enabled", False)
    db.commit()
    assert app_settings.flag("alpaca_market_stream_enabled", default=True) is False


def test_get_override_reports_none_when_unset(monkeypatch):
    db = _session()
    assert app_settings.get_override(db, "webull_market_stream_enabled") is None
    app_settings.set_flag(db, "webull_market_stream_enabled", True)
    db.commit()
    assert app_settings.get_override(db, "webull_market_stream_enabled") is True


def test_set_flag_busts_cache(monkeypatch):
    db = _session()
    _patch_sessionlocal(monkeypatch, db)
    assert app_settings.flag("k", default=False) is False  # caches default
    app_settings.set_flag(db, "k", True)
    db.commit()
    assert app_settings.flag("k", default=False) is True   # cache was busted


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
