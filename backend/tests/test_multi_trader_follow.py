"""Multi-trader following: the fanout must select a subscriber for EVERY trader
they follow (via subscriber_follows), gated by their single global copy_enabled.

Mirrors the exact JOIN get_subscribers_for_trader uses, on in-memory SQLite (the
real SubscriberSettings has JSONB columns SQLite can't render, so we use minimal
tables that carry just the join + gate columns).

Standalone (`.venv/bin/python tests/test_multi_trader_follow.py`) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text


def _db():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE subscriber_settings (user_id TEXT PRIMARY KEY, copy_enabled INT)"))
        c.execute(text("CREATE TABLE subscriber_follows (subscriber_id TEXT, trader_id TEXT)"))
        # S follows BOTH traders A and B, copy ON.
        c.execute(text("INSERT INTO subscriber_settings VALUES ('S', 1)"))
        c.execute(text("INSERT INTO subscriber_follows VALUES ('S','A')"))
        c.execute(text("INSERT INTO subscriber_follows VALUES ('S','B')"))
        # T follows A, copy OFF (global switch) — must be excluded.
        c.execute(text("INSERT INTO subscriber_settings VALUES ('T', 0)"))
        c.execute(text("INSERT INTO subscriber_follows VALUES ('T','A')"))
    return eng


def _subs_for(eng, trader):
    """The fanout's selection: subscribers who follow `trader` AND have copy on."""
    with eng.begin() as c:
        rows = c.execute(text(
            "SELECT ss.user_id FROM subscriber_settings ss "
            "JOIN subscriber_follows sf ON sf.subscriber_id = ss.user_id "
            "WHERE sf.trader_id = :t AND ss.copy_enabled = 1"
        ), {"t": trader}).fetchall()
    return sorted(r[0] for r in rows)


def test_subscriber_receives_from_every_followed_trader():
    """The core of multi-trader: S follows A and B → S is selected for BOTH."""
    eng = _db()
    assert _subs_for(eng, "A") == ["S"]
    assert _subs_for(eng, "B") == ["S"]   # <-- the whole point: also gets B's trades


def test_global_copy_off_excludes_from_all_traders():
    """copy_enabled is the single global switch — T (off) is never selected."""
    eng = _db()
    assert "T" not in _subs_for(eng, "A")


def test_unfollowed_trader_selects_nobody():
    eng = _db()
    assert _subs_for(eng, "C") == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll multi-trader follow tests passed.")
