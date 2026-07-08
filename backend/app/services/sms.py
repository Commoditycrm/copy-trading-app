"""SMS + phone verification via Twilio.

Mirrors services/email.py: no SDK, just ``httpx`` (already required) against
Twilio's REST API, and a graceful keyless fallback so dev/QA flows work
without credentials — when the Twilio creds are blank we log instead of
sending. Never raises; every function is best-effort and returns a bool /
status so callers (background tasks, notification dispatch) can't be crashed
by Twilio being down.

Two capabilities:
  * send_sms()                — outbound SMS (the notification channel), via
                                the Messages API + a From number.
  * start/check_phone_verification() — OTP over Twilio Verify, which owns code
                                generation, delivery, expiry and rate limiting
                                so we don't store codes ourselves.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_VERIFY_START_URL = "https://verify.twilio.com/v2/Services/{svc}/Verifications"
_VERIFY_CHECK_URL = "https://verify.twilio.com/v2/Services/{svc}/VerificationCheck"
_TIMEOUT = 10.0


def sms_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number)


def verify_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_verify_service_sid)


def send_sms(to: str, body: str) -> bool:
    """Send one SMS. Returns True on accept (HTTP 2xx). Never raises.

    Keyless dev/QA: logs the message at WARNING and returns False so flows
    stay testable without Twilio configured."""
    s = get_settings()
    if not sms_configured():
        log.warning("sms: Twilio not configured; NOT sending. to=%s body=%r", to, body)
        return False
    try:
        resp = httpx.post(
            _MESSAGES_URL.format(sid=s.twilio_account_sid),
            data={"From": s.twilio_from_number, "To": to, "Body": body},
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        log.exception("sms: Twilio request failed for to=%s", to)
        return False
    if resp.status_code // 100 == 2:
        log.info("sms: sent to=%s status=%s", to, resp.status_code)
        return True
    log.error("sms: Twilio rejected to=%s status=%s body=%s", to, resp.status_code, resp.text[:400])
    return False


def start_phone_verification(to: str) -> tuple[bool, str]:
    """Kick off a Twilio Verify OTP to ``to`` (SMS channel). Returns
    (ok, detail). ok=False with a human-ish detail on misconfig / Twilio
    error so the API can surface why."""
    s = get_settings()
    if not verify_configured():
        log.warning("sms: Twilio Verify not configured; NOT sending OTP to=%s", to)
        return False, "sms_not_configured"
    try:
        resp = httpx.post(
            _VERIFY_START_URL.format(svc=s.twilio_verify_service_sid),
            data={"To": to, "Channel": "sms"},
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        log.exception("sms: Verify start failed for to=%s", to)
        return False, "twilio_unreachable"
    if resp.status_code // 100 == 2:
        log.info("sms: verification started to=%s", to)
        return True, "sent"
    log.error("sms: Verify start rejected to=%s status=%s body=%s", to, resp.status_code, resp.text[:400])
    return False, f"twilio_error_{resp.status_code}"


def check_phone_verification(to: str, code: str) -> bool:
    """Check an OTP ``code`` for ``to`` against Twilio Verify. Returns True
    only when Twilio reports status "approved". Never raises."""
    s = get_settings()
    if not verify_configured():
        return False
    try:
        resp = httpx.post(
            _VERIFY_CHECK_URL.format(svc=s.twilio_verify_service_sid),
            data={"To": to, "Code": code},
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        log.exception("sms: Verify check failed for to=%s", to)
        return False
    if resp.status_code // 100 != 2:
        log.warning("sms: Verify check non-2xx to=%s status=%s", to, resp.status_code)
        return False
    try:
        approved = resp.json().get("status") == "approved"
    except Exception:  # noqa: BLE001
        approved = False
    log.info("sms: verification check to=%s approved=%s", to, approved)
    return approved
