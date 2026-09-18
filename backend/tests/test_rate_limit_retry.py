"""Tests for the broker 429 throttle detector that drives the inline
rate-limit retry in the mirror-placement path (copy_engine._place_one).

A 429 "Request was throttled" must be recognised (→ waited-out + retried, so a
1-second throttle can't turn a subscriber's close into a REJECTED), while real
rejections (buying power, options-not-eligible, conflicts) must NOT be — those
are not transient and re-placing would just fail again (or double-place).

Covers BOTH broker vocabularies. SnapTrade says it in prose and puts the number
under `status_code`; Webull's SDK raises every non-2xx as a ServerException
stringified as "HTTP Status: 429, Code: TOO_MANY_REQUESTS, ..." — underscored,
and with a different key for the status. The original literal-substring matcher
caught the first and missed the second entirely, so a throttled direct-Webull
mirror close went straight to REJECTED and stranded the subscriber.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.order_retry import classify_error, is_rate_limit_error


# The verbatim prod string (QQQ 2026-08-11).
_REAL_429 = ("SnapTrade place_mleg_order: {'detail': 'Request was throttled. "
             "Expected available in 1 second.', 'status_code': 429, 'code': '0000'}")

# What a throttled Webull call actually looks like by the time it reaches us:
# webull/core/client.py turns any non-2xx into a ServerException, and
# ServerException.__str__ is "HTTP Status: %s, Code: %s, Msg: %s, RequestID: %s".
#
# Msg is EMPTY here on purpose, and that is the whole point of the fixture:
# _parse_error_info_from_response_body defaults error_msg to "" unless the body
# carries a `message` key, so the only throttle signal in the string is the
# underscored code and the "HTTP Status:" number. An earlier draft of this test
# put a prose "too many requests" in Msg and passed against the OLD matcher —
# proving nothing.
_REAL_WEBULL_429 = (
    "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: , RequestID: 8f3c1a90b2"
)

# Same call, but Webull did populate a body message. Kept separate so the strict
# fixture above can't be "fixed" by loosening it.
_WEBULL_429_WITH_MSG = (
    "HTTP Status: 429, Code: TOO_MANY_REQUESTS, "
    "Msg: Request frequency exceeds the limit, RequestID: 8f3c1a90b2"
)


def test_matches_real_snaptrade_429():
    assert is_rate_limit_error(Exception(_REAL_429)) is True


def test_matches_real_webull_429():
    """The regression. Underscored code + 'HTTP Status:' rather than
    'status_code', so none of the old literal spellings fired."""
    assert is_rate_limit_error(Exception(_REAL_WEBULL_429)) is True


def test_matches_webull_429_with_a_body_message():
    assert is_rate_limit_error(Exception(_WEBULL_429_WITH_MSG)) is True


def test_matches_webull_throttle_wrapped_by_our_adapter():
    """WebullAdapter._error_text formats its own failures as 'HTTP <code> <msg>'
    — the shape _raise_for_status produces if a throttle ever arrives as a
    non-raising response rather than a ServerException."""
    assert is_rate_limit_error(
        RuntimeError("webull place_order failed: HTTP 429 TOO_MANY_REQUESTS")
    ) is True


def test_matches_throttle_variants():
    assert is_rate_limit_error(Exception("Request was throttled")) is True
    assert is_rate_limit_error(Exception("HTTP 429 Too Many Requests")) is True
    assert is_rate_limit_error(Exception("rate limit exceeded")) is True
    assert is_rate_limit_error(Exception('{"status_code": 429}')) is True


def test_word_separators_do_not_matter():
    """Brokers spell the same condition with spaces, underscores and hyphens."""
    for spelling in ("TOO_MANY_REQUESTS", "too many requests", "Too-Many-Requests",
                     "RATE_LIMIT_EXCEEDED", "rate limit", "rate-limited"):
        assert is_rate_limit_error(Exception(spelling)) is True, spelling


def test_ignores_real_rejections():
    # These are NOT throttles — they must fall through to the normal reject path,
    # never get blind-retried.
    assert is_rate_limit_error(Exception("Insufficient buying power on this account.")) is False
    assert is_rate_limit_error(Exception("insufficient qty available")) is False
    assert is_rate_limit_error(Exception("your account is not eligible to trade options")) is False
    assert is_rate_limit_error(Exception("wash trade detected")) is False
    assert is_rate_limit_error(Exception("order chain not fully replaced")) is False


def test_does_not_match_a_bare_number_that_happens_to_be_429():
    """A false positive RE-PLACES the order, so 429 only counts when it is
    positionally a status — not a price, quantity or id that contains it."""
    assert is_rate_limit_error(Exception("Insufficient buying power for 429.50")) is False
    assert is_rate_limit_error(Exception("order rejected: qty 429 exceeds holding")) is False
    assert is_rate_limit_error(Exception("broker_order_id WB1429 not found")) is False
    assert is_rate_limit_error(Exception("HTTP Status: 4291")) is False


def test_does_not_match_the_webull_auth_lockout():
    """VERIFY_FAILURE_EXCEED_LIMIT is a credentials lockout, not a throttle.
    Retrying it inline would hammer Webull's auth endpoint and deepen it."""
    assert is_rate_limit_error(
        Exception("HTTP Status: 403, Code: VERIFY_FAILURE_EXCEED_LIMIT, Msg: ")
    ) is False


def test_webull_429_also_classifies_as_transient():
    """Belt and braces: if the inline retries are exhausted, the order must still
    route to RETRY_PENDING rather than a hard REJECTED."""
    cls = classify_error(Exception(_REAL_WEBULL_429))
    assert cls.transient is True
    assert cls.clean_message is None   # not mislabelled as user-fixable


def test_backoff_config_is_sane():
    """The retry budget must actually outlast a '1 second' throttle."""
    import app.services.copy_engine as ce
    assert ce._RATE_LIMIT_ATTEMPTS >= 3
    # Total wait across attempts (escalating) comfortably exceeds ~1s.
    total = sum(ce._RATE_LIMIT_BACKOFF_S * (a + 1) for a in range(ce._RATE_LIMIT_ATTEMPTS - 1))
    assert total >= 2.0
