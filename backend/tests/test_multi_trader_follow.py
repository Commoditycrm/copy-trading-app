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


# ── source_trader_id attribution ──────────────────────────────────────────────
# Mirrors the migration backfill + the copy_engine/trades creation rule:
#   mirror (parent set) → source = parent.user_id (the trader)
#   root/manual         → source = self
def _orders_db():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE orders (id TEXT PRIMARY KEY, user_id TEXT, "
            "parent_order_id TEXT, source_trader_id TEXT)"
        ))
        # Trader A's own root order → source = self.
        c.execute(text("INSERT INTO orders VALUES ('o_a','A',NULL,'A')"))
        # Subscriber S's mirror of A's order → source = A (the trader).
        c.execute(text("INSERT INTO orders VALUES ('o_s','S','o_a','A')"))
        # Subscriber S's mirror of B's order → source = B.
        c.execute(text("INSERT INTO orders VALUES ('o_b','B',NULL,'B')"))
        c.execute(text("INSERT INTO orders VALUES ('o_sb','S','o_b','B')"))
    return eng


def test_source_trader_on_mirror_is_the_trader():
    eng = _orders_db()
    with eng.begin() as c:
        # Every mirror's source_trader_id equals its parent's owner (the trader).
        bad = c.execute(text(
            "SELECT o.id FROM orders o JOIN orders p ON o.parent_order_id = p.id "
            "WHERE o.source_trader_id <> p.user_id"
        )).fetchall()
    assert bad == [], f"mirrors mis-attributed: {bad}"


def test_source_trader_on_root_is_self():
    eng = _orders_db()
    with eng.begin() as c:
        bad = c.execute(text(
            "SELECT id FROM orders WHERE parent_order_id IS NULL "
            "AND source_trader_id <> user_id"
        )).fetchall()
    assert bad == [], f"root orders mis-attributed: {bad}"


def test_subscriber_can_differentiate_orders_by_trader():
    """The whole point: S's orders carry which trader each came from."""
    eng = _orders_db()
    with eng.begin() as c:
        rows = c.execute(text(
            "SELECT source_trader_id FROM orders WHERE user_id='S' ORDER BY source_trader_id"
        )).fetchall()
    assert sorted(r[0] for r in rows) == ["A", "B"]


# ── membership gates (retry eligibility, trader roster) key off follows ───────
def _is_following(eng, subscriber, trader):
    with eng.begin() as c:
        return c.execute(text(
            "SELECT 1 FROM subscriber_follows WHERE subscriber_id=:s AND trader_id=:t LIMIT 1"
        ), {"s": subscriber, "t": trader}).first() is not None


def test_retry_gate_passes_for_secondary_followed_trader():
    """S's primary would be A, but they ALSO follow B: a retry on a B-originated
    mirror must NOT be dropped as 'no_longer_following'."""
    eng = _db()  # S follows A and B
    assert _is_following(eng, "S", "B") is True


def test_trader_roster_includes_secondary_follower():
    """B's subscriber roster (join on subscriber_follows) includes S even though
    S's primary is A."""
    eng = _db()
    with eng.begin() as c:
        roster = c.execute(text(
            "SELECT ss.user_id FROM subscriber_settings ss "
            "JOIN subscriber_follows sf ON sf.subscriber_id = ss.user_id "
            "WHERE sf.trader_id = 'B'"
        )).fetchall()
    assert sorted(r[0] for r in roster) == ["S"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll multi-trader follow tests passed.")
