"""Transactional SMS via the Twilio REST API.

No new dependency — like services/email.py this POSTs to Twilio over ``httpx``
(already required) instead of pulling in the ``twilio`` SDK.

Dev / QA without credentials
----------------------------
When ``twilio_account_sid`` / ``twilio_auth_token`` are blank we DON'T attempt
a send — we log the message at WARNING so SMS flows stay testable without a
Twilio account.

Sender
------
Prefer a Messaging Service SID (``twilio_messaging_service_sid``, MG…) — it owns
the sender pool, opt-out handling and retries. If unset we fall back to
``twilio_from_number`` (a single SMS-capable Twilio number, E.164). One of the
two must be set or the send is a no-op.

Best-effort: never raises. Intended call site is a FastAPI BackgroundTask so a
slow/failing send never blocks the request.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_API_ROOT = "https://api.twilio.com/2010-04-01"
_TIMEOUT = 10.0

_BRAND = "Kopyya"
_OPT_OUT = "Reply STOP to opt out."

# Anything outside GSM-7 forces the WHOLE message to UCS-2, which drops a
# segment from 160 chars to 70 — so one stray em dash can double the bill.
# Fold the non-GSM characters our notification copy actually emits.
_GSM_FOLD = str.maketrans({
    "—": "-",    # em dash
    "–": "-",    # en dash
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
})


def compose(body: str) -> str:
    """Brand + opt-out wrap required for registered A2P 10DLC traffic.

    Carriers filter messages that don't identify the sender or carry opt-out
    instructions, and our 10DLC campaign registration declares that every
    message has both — so this MUST stay in sync with the sample messages
    submitted to Twilio. Applied centrally so no call site can forget it.

    Both additions are idempotent: a caller that already branded or already
    included the opt-out line won't get it twice.
    """
    text = body.translate(_GSM_FOLD).strip()
    if not text.startswith(f"{_BRAND}:"):
        text = f"{_BRAND}: {text}"
    if _OPT_OUT not in text:
        text = f"{text} {_OPT_OUT}"
    return text


def send_sms(to: str, body: str) -> bool:
    """Send one SMS to ``to`` (E.164, e.g. "+15551234567"). Returns True on a
    Twilio accept (HTTP 2xx), False otherwise. Never raises — SMS is
    best-effort; callers must not crash if Twilio is down."""
    s = get_settings()
    text = compose(body)

    if not (s.twilio_account_sid and s.twilio_auth_token):
        # No credentials — log instead of sending so dev/QA flows work.
        # Log the composed text so what dev sees is what prod would send.
        log.warning("sms: TWILIO creds not set; NOT sending. to=%s body=%r", to, text)
        return False

    # Sender: a Messaging Service SID wins; else a from-number. One is required.
    data = {"To": to, "Body": text}
    if s.twilio_messaging_service_sid:
        data["MessagingServiceSid"] = s.twilio_messaging_service_sid
    elif s.twilio_from_number:
        data["From"] = s.twilio_from_number
    else:
        log.error(
            "sms: no sender configured (set TWILIO_MESSAGING_SERVICE_SID or "
            "TWILIO_FROM_NUMBER); NOT sending. to=%s", to,
        )
        return False

    url = f"{_API_ROOT}/Accounts/{s.twilio_account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            data=data,
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        log.exception("sms: Twilio request failed for to=%s", to)
        return False

    if resp.status_code // 100 == 2:
        log.info("sms: sent to=%s status=%s", to, resp.status_code)
        return True
    log.error(
        "sms: Twilio rejected to=%s status=%s body=%s",
        to, resp.status_code, resp.text[:500],
    )
    return False
