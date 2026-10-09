"""GET /api/trades?filled_from= — orders by FILL time, whenever they were placed.

The Positions page's "Closed today" table relies on it: a take-profit placed
yesterday that fills today must count as today's exit, which the placement-
based `from` filter would miss.
"""
import uuid
from datetime import date

from app.api import trades


class _Result:
    def scalars(self):
        return iter(())


class _DB:
    def __init__(self):
        self.stmt = None

    def execute(self, stmt):
        self.stmt = stmt
        return _Result()


class _User:
    id = uuid.uuid4()


def _sql(**kw) -> tuple[str, dict]:
    db = _DB()
    args = {"from_": None, "to": None, "filled_from": None, "limit": 500}
    args.update(kw)
    trades.list_trades(db=db, user=_User(), **args)
    compiled = db.stmt.compile()
    return str(compiled), compiled.params


def test_filled_from_filters_on_fill_time_not_placement():
    sql, params = _sql(filled_from=date(2026, 10, 1))
    where = sql.split("WHERE", 1)[1]
    assert "coalesce(orders.broker_filled_at, orders.closed_at) >=" in where
    starts = [v for v in params.values() if hasattr(v, "tzinfo") and v.tzinfo is not None]
    # Midnight ET on the day asked for (04:00 UTC in October).
    assert any(v.date() == date(2026, 10, 1) and v.utcoffset().total_seconds() == -4 * 3600 for v in starts)
    assert "coalesce(orders.submitted_at, orders.created_at) >=" not in where


def test_without_filled_from_no_fill_time_filter():
    sql, _ = _sql()
    assert "broker_filled_at, orders.closed_at) >=" not in sql.split("WHERE", 1)[1]
