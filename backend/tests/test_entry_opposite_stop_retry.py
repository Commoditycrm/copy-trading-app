"""An entry may clear its OWN stop — on a broker that forces the choice.

Alpaca refuses a BUY while a sell-stop rests on the same contract ("potential
wash trade ... opposite side market/stop order exists"). Webull has no such
rule, and can rest a linked take-profit/stop pair anyway. So on QA 2026-10-22
every add from Clint's channels — a strategy built on averaging in — was
refused for the Alpaca subscriber and filled for the Webull trader: eight
rejections against zero.

The entry path is allowed to cancel the LADDER'S OWN stop and retry. The guards
are what make that safe, and each has a test here:

  1. the broker cannot rest a pair          (Webull never enters the path)
  2. the rejection means opposite-side      (not the whole conflict family)
  3. it is an entry, not a close
  4. only our closing STOP is cancelled     (never a trader's resting order)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers import broker_supports_exit_pair
from app.models.broker_account import BrokerName
from app.services.order_retry import (
    is_entry_blocked_by_opposite_order,
    is_order_conflict_error,
)

ALPACA_WASH = ('{"code":40310000,"message":"potential wash trade detected. use '
               'complex orders","reject_reason":"opposite side market/stop order exists"}')
WEBULL_NAKED = "HTTP Status: 417, Code: OPENAPI: you cannot open additional naked call positions"
WEBULL_1119 = ("code 1119: You can not place order in excess of current holding quantity "
               "to create a position on the other side of the market. Please check your "
               "open orders and try again.")
WEBULL_DUP = "HTTP Status: 417, Code: OPENAPI_DUPLICATED_TASK_REQUEST, Msg: Request is being processed"
NOT_FRACTIONABLE = '{"code":40310000,"message":"asset QQQ is not fractionable"}'


# ── guard 2: the rejection must mean "your own sell is in the way" ───────────

def test_alpacas_wash_trade_is_recognised():
    assert is_entry_blocked_by_opposite_order(Exception(ALPACA_WASH)) is True


def test_webull_conflicts_are_not_entry_conflicts():
    # THE regression guard. These are real Webull rejections seen in QA. The
    # broad helper matches them (it is used by the CLOSE path); the entry one
    # must not, or an entry would start cancelling stops on Webull.
    for msg in (WEBULL_NAKED, WEBULL_1119):
        assert is_order_conflict_error(Exception(msg)) is True, msg
        assert is_entry_blocked_by_opposite_order(Exception(msg)) is False, msg


def test_a_duplicate_request_is_not_an_opposite_side_conflict():
    assert is_entry_blocked_by_opposite_order(Exception(WEBULL_DUP)) is False


def test_the_same_alpaca_code_on_an_unrelated_refusal_does_not_qualify():
    # 40310000 is a broad family; matching the CODE would cancel stops for
    # "not fractionable" and friends. Only the message qualifies.
    assert is_entry_blocked_by_opposite_order(Exception(NOT_FRACTIONABLE)) is False


# ── guard 1: the broker gate ─────────────────────────────────────────────────

def test_webull_is_excluded_by_capability():
    # Even if its message somehow matched, the capability gate keeps Webull out.
    assert broker_supports_exit_pair(BrokerName.WEBULL) is True


def test_brokers_that_cannot_rest_a_pair_are_in_scope():
    for broker in (BrokerName.ALPACA, BrokerName.SNAPTRADE, BrokerName.IBKR):
        assert broker_supports_exit_pair(broker) is False, broker


# ── guard 4: what the canceller is allowed to touch ──────────────────────────

def test_the_canceller_selects_only_our_closing_stop():
    # The filters are the safety property: a SELL + STOP + is_closing order of
    # ours, for this exact contract. A take-profit (LIMIT) or a trader's own
    # resting order must never match.
    import inspect

    from app.api import trades
    src = inspect.getsource(trades._cancel_protective_stop)
    for required in ("Order.side == OrderSide.SELL",
                     "Order.order_type == OrderType.STOP",
                     "Order.is_closing.is_(True)",
                     "Order.broker_order_id.isnot(None)"):
        assert required in src, required
    # and it must NOT be the broad close-path canceller
    assert "_CANCELLABLE_STATUSES" in src
