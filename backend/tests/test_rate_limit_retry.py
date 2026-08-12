"""Tests for the SnapTrade 429 throttle detector that drives the inline
rate-limit retry in the mirror-placement path (copy_engine._place_one).

A 429 "Request was throttled" must be recognised (→ waited-out + retried, so a
1-second throttle can't turn a subscriber's close into a REJECTED), while real
rejections (buying power, options-not-eligible, conflicts) must NOT be — those
are not transient and re-placing would just fail again (or double-place).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.order_retry import is_rate_limit_error


# The verbatim prod string (QQQ 2026-08-11).
_REAL_429 = ("SnapTrade place_mleg_order: {'detail': 'Request was throttled. "
             "Expected available in 1 second.', 'status_code': 429, 'code': '0000'}")


def test_matches_real_snaptrade_429():
    assert is_rate_limit_error(Exception(_REAL_429)) is True


def test_matches_throttle_variants():
    assert is_rate_limit_error(Exception("Request was throttled")) is True
    assert is_rate_limit_error(Exception("HTTP 429 Too Many Requests")) is True
    assert is_rate_limit_error(Exception("rate limit exceeded")) is True
    assert is_rate_limit_error(Exception('{"status_code": 429}')) is True


def test_ignores_real_rejections():
    # These are NOT throttles — they must fall through to the normal reject path,
    # never get blind-retried.
    assert is_rate_limit_error(Exception("Insufficient buying power on this account.")) is False
    assert is_rate_limit_error(Exception("insufficient qty available")) is False
    assert is_rate_limit_error(Exception("your account is not eligible to trade options")) is False
    assert is_rate_limit_error(Exception("wash trade detected")) is False
    assert is_rate_limit_error(Exception("order chain not fully replaced")) is False


def test_backoff_config_is_sane():
    """The retry budget must actually outlast a '1 second' throttle."""
    import app.services.copy_engine as ce
    assert ce._RATE_LIMIT_ATTEMPTS >= 3
    # Total wait across attempts (escalating) comfortably exceeds ~1s.
    total = sum(ce._RATE_LIMIT_BACKOFF_S * (a + 1) for a in range(ce._RATE_LIMIT_ATTEMPTS - 1))
    assert total >= 2.0
