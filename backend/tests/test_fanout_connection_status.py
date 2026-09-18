"""The fanout must not place mirrors on a broker connection that cannot trade.

`connection_status` was never consulted when picking a subscriber's accounts, so
a disconnected or credentials-invalid row kept receiving mirrors on every trader
trade. Each attempt cost a REJECTED order row, a copy.rejected notification, and
an SMS for anyone opted in — forever, because nothing in the app moves an account
back into a usable state on its own: the only writes of this column set it to
"connected" on a successful connect.

Measured on QA before the filter, over 30 days:

    connected           FILLED      761
    connected           REJECTED     86
    credentials_invalid REJECTED    148     <- 100% rejected, never filled
    disconnected        REJECTED      7     <- 100% rejected, never filled

That 100% is what makes skipping safe rather than a silent pause. The contrast
with services.balance_sync is deliberate: it refuses to FLIP this column on an
auth error precisely so a misclassification can't stop someone copying, which is
why a row that IS in this state got there by an explicit disconnect.
"""
import os, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal
import app.services.copy_engine as ce


class _Acct:
    def __init__(self, status):
        self.id = uuid.uuid4()
        self.connection_status = status


def _filter(accts):
    """The expression the fanout applies when choosing a subscriber's accounts."""
    return [a for a in accts
            if getattr(a, "connection_status", "connected") == "connected"]


def test_connected_account_is_kept():
    a = _Acct("connected")
    assert _filter([a]) == [a]


def test_credentials_invalid_is_dropped():
    assert _filter([_Acct("credentials_invalid")]) == []


def test_disconnected_is_dropped():
    assert _filter([_Acct("disconnected")]) == []


def test_pending_is_dropped():
    """A half-finished connect can't place either."""
    assert _filter([_Acct("pending")]) == []


def test_mixed_keeps_only_the_usable_one():
    good, bad = _Acct("connected"), _Acct("credentials_invalid")
    assert _filter([bad, good]) == [good]


def test_missing_attribute_defaults_to_usable():
    """Cache DTOs and ORM rows both carry the field today, but the fanout must
    not start dropping everyone if some future shape omits it."""
    class _Bare:
        id = uuid.uuid4()
    b = _Bare()
    assert _filter([b]) == [b]


def test_batched_path_filters_in_sql():
    """The two paths must agree, or which mirrors get placed would depend on the
    subscriber count (the batch threshold)."""
    import inspect
    src = inspect.getsource(ce.fanout_async)
    assert 'BrokerAccount.connection_status == "connected"' in src
    assert 'getattr(a, "connection_status", "connected") == "connected"' in src


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print(f"PASS  {n}")
    print("\nAll fanout connection-status tests passed.")
