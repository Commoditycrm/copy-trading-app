"""Regression test for the EOD 0DTE new-order lockout that silently no-op'd.

Prod symptom: the per-subscriber "auto-close expiring (0DTE) options" feature
FLATTENED existing same-day-expiry positions in the final window (the sweep reads
SubscriberSettings from the DB), but the paired NEW-ORDER LOCKOUT still let fresh
same-day-expiry mirrors through. Cause: the fanout reads the subscriber from the
Redis cache (`CachedSubscriber`), and that object dropped `eod_autoclose_enabled`
/ `eod_autoclose_minutes` — so the lockout's `getattr(sub, "eod_autoclose_enabled",
False)` was ALWAYS False and the check could never fire.

Fix: carry both fields through CachedSubscriber and its Redis (de)serialization.

Standalone (`.venv/bin/python tests/test_eod_cache.py`) or under pytest.
"""
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.cache import CachedSubscriber, _sub_to_dict, _sub_from_dict


def _cs(**kw) -> CachedSubscriber:
    base = dict(
        user_id=uuid.uuid4(),
        following_trader_id=uuid.uuid4(),
        copy_enabled=True,
        multiplier=Decimal("1"),
        daily_loss_limit=None,
    )
    base.update(kw)
    return CachedSubscriber(**base)


def test_cached_subscriber_carries_eod_fields():
    """The exact read the fanout lockout does — must see the real value now."""
    cs = _cs(eod_autoclose_enabled=True, eod_autoclose_minutes=10)
    assert getattr(cs, "eod_autoclose_enabled", False) is True   # was always False (bug)
    assert getattr(cs, "eod_autoclose_minutes", 15) == 10


def test_eod_fields_survive_redis_round_trip():
    """The cache serializes to Redis JSON and back — fields must persist."""
    cs = _cs(eod_autoclose_enabled=True, eod_autoclose_minutes=7)
    d = _sub_to_dict(cs)
    assert d["eod_autoclose_enabled"] is True
    assert d["eod_autoclose_minutes"] == 7
    cs2 = _sub_from_dict(d)
    assert cs2.eod_autoclose_enabled is True
    assert cs2.eod_autoclose_minutes == 7


def test_old_cache_entry_without_eod_keys_defaults_safely():
    """A pre-fix cached payload (no eod keys) must not crash and default to off."""
    d = _sub_to_dict(_cs())
    d.pop("eod_autoclose_enabled")
    d.pop("eod_autoclose_minutes")
    cs = _sub_from_dict(d)
    assert cs.eod_autoclose_enabled is False
    assert cs.eod_autoclose_minutes == 15


def test_default_subscriber_lockout_is_opt_in():
    """No opt-in → disabled, so the lockout stays opt-in (never fires uninvited)."""
    assert _cs().eod_autoclose_enabled is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll EOD-cache tests passed.")
