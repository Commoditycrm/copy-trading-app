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
import re
from dataclasses import dataclass

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
    # "Kopyya:" or an environment-labelled "Kopyya QA:" both name the sender
    if not re.match(rf"{_BRAND}\b[^:\n]{{0,12}}:", text):
        text = f"{_BRAND}: {text}"
    if _OPT_OUT not in text:
        text = f"{text} {_OPT_OUT}"
    return text


@dataclass(frozen=True)
class SmsResult:
    """What Twilio said about one send. ``transient`` = worth trying again
    (network, 429, 5xx); a 4xx is Twilio refusing this message for good."""
    ok: bool
    sid: str | None = None
    status: str | None = None          # Twilio message status, e.g. "queued"
    error_code: int | None = None
    error: str | None = None
    transient: bool = False
    num_segments: int | None = None


def send_sms(to: str, body: str) -> bool:
    """Send one SMS to ``to`` (E.164, e.g. "+15551234567"). Returns True on a
    Twilio accept (HTTP 2xx), False otherwise. Never raises — SMS is
    best-effort; callers must not crash if Twilio is down."""
    return send_sms_detailed(to, body).ok


def send_sms_detailed(to: str, body: str) -> SmsResult:
    """send_sms, keeping Twilio's message SID and error so a caller can track
    delivery and decide whether to retry. Never raises."""
    s = get_settings()
    text = compose(body)

    if not (s.twilio_account_sid and s.twilio_auth_token):
        # No credentials — log instead of sending so dev/QA flows work.
        # Log the composed text so what dev sees is what prod would send.
        log.warning("sms: TWILIO creds not set; NOT sending. to=%s body=%r", to, text)
        return SmsResult(ok=False, error="twilio not configured")

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
        return SmsResult(ok=False, error="no sender configured")

    url = f"{_API_ROOT}/Accounts/{s.twilio_account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            data=data,
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("sms: Twilio request failed for to=%s", to)
        return SmsResult(ok=False, error=f"request failed ({type(exc).__name__})", transient=True)

    body_json = _json(resp)
    if resp.status_code // 100 == 2:
        log.info("sms: sent to=%s status=%s", to, resp.status_code)
        return SmsResult(ok=True, sid=body_json.get("sid"), status=body_json.get("status"),
                         num_segments=_int(body_json.get("num_segments")))
    log.error(
        "sms: Twilio rejected to=%s status=%s body=%s",
        to, resp.status_code, resp.text[:500],
    )
    return SmsResult(ok=False, error_code=_int(body_json.get("code")),
                     error=str(body_json.get("message") or f"HTTP {resp.status_code}")[:300],
                     transient=resp.status_code == 429 or resp.status_code >= 500)


def fetch_message(sid: str) -> dict | None:
    """Twilio's current record for one message (status, error_code, price,
    num_segments). None when it can't be read. Never raises."""
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token and sid):
        return None
    try:
        resp = httpx.get(f"{_API_ROOT}/Accounts/{s.twilio_account_sid}/Messages/{sid}.json",
                         auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001
        log.warning("sms: status lookup failed for sid=%s", sid)
        return None
    return _json(resp) if resp.status_code == 200 else None


def _json(resp) -> dict:  # noqa: ANN001
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _int(v) -> int | None:  # noqa: ANN001
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
