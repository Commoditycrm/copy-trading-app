"""Transactional email via the SendGrid Web API v3.

No new dependency — this uses ``httpx`` (already required) to POST to
SendGrid's ``/v3/mail/send`` endpoint instead of pulling in the SendGrid
SDK or opening an SMTP connection (SMTP ports are commonly blocked on
cloud hosts; the HTTPS API is not).

Dev / QA without a key
----------------------
When ``settings.sendgrid_api_key`` is blank we DON'T attempt a send — we
log the message (and, for password resets, the reset link) at WARNING so
the flow stays fully testable without credentials. That means
``/api/auth/forgot-password`` works end-to-end on a box with no SendGrid
key: grab the link from the worker/web logs.

Sender identity
---------------
``settings.email_from`` must be a verified single sender or an
authenticated domain in SendGrid, otherwise the API returns 403.

Intended call site: a FastAPI ``BackgroundTasks`` job, so the request
returns immediately and a slow/failing send never blocks the user.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
_TIMEOUT = 10.0


def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    *,
    template_id: str | None = None,
    dynamic_data: dict[str, object] | None = None,
) -> bool:
    """Send one email. Returns True on accept (HTTP 2xx), False otherwise.

    When ``template_id`` is given, the email is sent via a SendGrid Dynamic
    Template: the template owns the subject + body and is rendered from
    ``dynamic_data`` (handlebars). The ``subject``/``html``/``text`` args are
    then used only for the keyless dev-log fallback. Otherwise the inline
    ``subject`` + ``html``/``text`` content is sent directly.

    Never raises — email is best-effort; callers (background tasks) should
    not crash if SendGrid is down. Failures are logged.
    """
    s = get_settings()
    if not s.sendgrid_api_key:
        # No key configured — log instead of sending so dev/QA flows work.
        # Prefer the dynamic data (contains the reset link) when templated.
        body = text or html
        if template_id and dynamic_data:
            body = "\n".join(f"{k}={v}" for k, v in dynamic_data.items())
        log.warning(
            "email: SENDGRID_API_KEY not set; NOT sending. to=%s subject=%r\n%s",
            to, subject, body,
        )
        return False

    if template_id:
        # Dynamic Template: subject + content come from the template itself.
        payload = {
            "personalizations": [
                {"to": [{"email": to}], "dynamic_template_data": dynamic_data or {}}
            ],
            "from": {"email": s.email_from, "name": s.email_from_name},
            "template_id": template_id,
        }
    else:
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": s.email_from, "name": s.email_from_name},
            "subject": subject,
            "content": (
                ([{"type": "text/plain", "value": text}] if text else [])
                + [{"type": "text/html", "value": html}]
            ),
        }
    try:
        resp = httpx.post(
            _SENDGRID_URL,
            json=payload,
            headers={"Authorization": f"Bearer {s.sendgrid_api_key}"},
            timeout=_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        log.exception("email: SendGrid request failed for to=%s", to)
        return False

    if resp.status_code // 100 == 2:
        log.info("email: sent to=%s subject=%r status=%s", to, subject, resp.status_code)
        return True
    log.error(
        "email: SendGrid rejected to=%s status=%s body=%s",
        to, resp.status_code, resp.text[:500],
    )
    return False


def send_password_reset_email(to: str, reset_link: str, display_name: str | None) -> bool:
    """Compose + send the password-reset email. Safe to call from a
    BackgroundTask. Returns the underlying send result."""
    s = get_settings()
    app = s.email_from_name
    name = (display_name or "").strip() or "there"
    mins = s.password_reset_token_minutes
    subject = f"Reset your {app} password"
    text = (
        f"Hi {name},\n\n"
        f"We received a request to reset the password for your {app} "
        "copy-trading account — the one you use to mirror your traders and "
        "manage your connected brokerages.\n\n"
        f"Choose a new password here (valid for {mins} minutes):\n"
        f"{reset_link}\n\n"
        f"For your security, {app} will never ask for your password or brokerage "
        "credentials by email. If you didn't request this, you can ignore this "
        "message — your password and copy-trading settings stay unchanged.\n\n"
        f"— The {app} team\n"
    )
    html = f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 12px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <tr><td align="center">
    <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,0.08)">
      <tr><td style="background:#0f172a;padding:22px 32px">
        <span style="color:#ffffff;font-size:20px;font-weight:800;letter-spacing:0.5px">{app}</span>
        <span style="color:#60a5fa;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Copy trading</span>
      </td></tr>
      <tr><td style="padding:32px 32px 8px">
        <h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a">Reset your password</h1>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">
          We received a request to reset the password for your <strong>{app}</strong>
          account — the one you use to mirror your traders and manage your connected
          brokerages. Choose a new password below. This link expires in
          <strong>{mins} minutes</strong>.</p>
        <p style="margin:28px 0">
          <a href="{reset_link}"
             style="background:#2563eb;color:#ffffff;text-decoration:none;padding:13px 26px;border-radius:8px;font-weight:600;font-size:15px;display:inline-block">
            Reset password
          </a>
        </p>
        <p style="margin:0 0 8px;font-size:13px;color:#64748b">If the button doesn't work, paste this link into your browser:</p>
        <p style="margin:0 0 8px;font-size:13px"><a href="{reset_link}" style="color:#2563eb;word-break:break-all">{reset_link}</a></p>
      </td></tr>
      <tr><td style="padding:8px 32px 28px">
        <div style="border-top:1px solid #e2e8f0;padding-top:16px">
          <p style="margin:0;font-size:13px;line-height:1.6;color:#64748b">
            🔒 For your security, {app} will <strong>never</strong> ask for your password or
            brokerage credentials by email. If you didn't request this reset, you can safely
            ignore this message — your password and copy-trading settings stay unchanged.</p>
        </div>
      </td></tr>
      <tr><td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0">
        <p style="margin:0;font-size:12px;color:#94a3b8">{app} · Automated copy trading</p>
        <p style="margin:4px 0 0;font-size:12px;color:#94a3b8">You received this email because a password reset was requested for this address.</p>
      </td></tr>
    </table>
  </td></tr>
</table>"""
    # Prefer a SendGrid Dynamic Template when one is configured; the inline
    # html/text above is kept as the keyless dev-log + no-template fallback.
    template_id = s.sendgrid_password_reset_template_id or None
    dynamic_data = {
        "reset_link": reset_link,
        "name": name,
        "app_name": s.email_from_name,
        "expiry_minutes": mins,
    }
    return send_email(
        to, subject, html, text, template_id=template_id, dynamic_data=dynamic_data
    )


def _shell(app: str, heading: str, body_html: str) -> str:
    """Shared branded card wrapper for the transactional emails below, so a
    new notification email is body copy + a link, not another 40-line table."""
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 12px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <tr><td align="center">
    <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,0.08)">
      <tr><td style="background:#0f172a;padding:22px 32px">
        <span style="color:#ffffff;font-size:20px;font-weight:800;letter-spacing:0.5px">{app}</span>
        <span style="color:#60a5fa;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Copy trading</span>
      </td></tr>
      <tr><td style="padding:32px 32px 24px">
        <h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a">{heading}</h1>
        {body_html}
      </td></tr>
      <tr><td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0">
        <p style="margin:0;font-size:12px;color:#94a3b8">{app} · Automated copy trading</p>
      </td></tr>
    </table>
  </td></tr>
</table>"""


def send_follow_request_email(
    to: str, trader_name: str | None, subscriber_name: str
) -> bool:
    """Notify a trader that a subscriber has requested to follow them. Safe to
    call from a BackgroundTask."""
    s = get_settings()
    app = s.email_from_name
    name = (trader_name or "").strip() or "there"
    link = f"{s.frontend_base_url}/settings"
    subject = f"{subscriber_name} requested to follow you on {app}"
    text = (
        f"Hi {name},\n\n"
        f"{subscriber_name} has requested to copy your trades on {app}.\n\n"
        f"Review and approve or decline the request here:\n{link}\n\n"
        f"— The {app} team\n"
    )
    body = f"""\
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">
          <strong>{subscriber_name}</strong> has requested to copy your trades on
          <strong>{app}</strong>. They won't mirror anything until you approve.</p>
        <p style="margin:24px 0">
          <a href="{link}" style="background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:15px;display:inline-block">
            Review request
          </a>
        </p>"""
    return send_email(to, subject, _shell(app, "New follow request", body), text)


def send_follow_decision_email(
    to: str, subscriber_name: str | None, trader_label: str, approved: bool
) -> bool:
    """Notify a subscriber that a trader approved or declined their follow
    request. Safe to call from a BackgroundTask."""
    s = get_settings()
    app = s.email_from_name
    name = (subscriber_name or "").strip() or "there"
    link = f"{s.frontend_base_url}/settings"
    if approved:
        subject = f"{trader_label} approved your follow request"
        lead = (
            f"<strong>{trader_label}</strong> approved your request — you're now "
            "following them. Turn on copy trading in your settings to start "
            "mirroring their trades.")
        text_lead = (
            f"{trader_label} approved your request — you're now following them. "
            "Turn on copy trading in your settings to start mirroring their trades.")
        cta = "Go to settings"
    else:
        subject = f"Update on your follow request to {trader_label}"
        lead = (
            f"<strong>{trader_label}</strong> declined your follow request. You "
            "can request a different trader anytime from your settings.")
        text_lead = (
            f"{trader_label} declined your follow request. You can request a "
            "different trader anytime from your settings.")
        cta = "Browse traders"
    text = f"Hi {name},\n\n{text_lead}\n\n{link}\n\n— The {app} team\n"
    body = f"""\
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">{lead}</p>
        <p style="margin:24px 0">
          <a href="{link}" style="background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:15px;display:inline-block">
            {cta}
          </a>
        </p>"""
    heading = "Follow request approved" if approved else "Follow request update"
    return send_email(to, subject, _shell(app, heading, body), text)


def send_email_change_verification(to: str, verify_link: str, display_name: str | None) -> bool:
    """Send the confirm-your-new-email link to the NEW address during an email
    change. The change doesn't take effect until this link is clicked."""
    s = get_settings()
    app = s.email_from_name
    name = (display_name or "").strip() or "there"
    subject = f"Confirm your new {app} email"
    text = (
        f"Hi {name},\n\n"
        f"We received a request to change the email on your {app} account to this "
        "address. Confirm it here to finish the change:\n"
        f"{verify_link}\n\n"
        "If you didn't request this, you can ignore this email — nothing changes "
        "until the link is clicked.\n\n"
        f"— The {app} team\n"
    )
    body = f"""\
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">
          We received a request to change the email on your <strong>{app}</strong> account to
          this address. Confirm it to finish the change — nothing changes until you do.</p>
        <p style="margin:24px 0">
          <a href="{verify_link}" style="background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:15px;display:inline-block">
            Confirm new email
          </a>
        </p>
        <p style="margin:0 0 8px;font-size:13px;color:#64748b">If the button doesn't work, paste this link into your browser:</p>
        <p style="margin:0;font-size:13px"><a href="{verify_link}" style="color:#2563eb;word-break:break-all">{verify_link}</a></p>"""
    return send_email(to, subject, _shell(app, "Confirm your new email", body), text)


def send_email_change_notice(to_old: str, new_email: str, display_name: str | None) -> bool:
    """Heads-up to the OLD address that an email change was requested, so the
    user is alerted if it wasn't them."""
    s = get_settings()
    app = s.email_from_name
    name = (display_name or "").strip() or "there"
    subject = f"Email change requested on your {app} account"
    text = (
        f"Hi {name},\n\n"
        f"Someone requested to change your {app} account email to {new_email}. "
        "The change only completes once the new address is confirmed.\n\n"
        "If this wasn't you, change your password immediately — your account may "
        "be compromised.\n\n"
        f"— The {app} team\n"
    )
    body = f"""\
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">
          Someone requested to change your <strong>{app}</strong> account email to
          <strong>{new_email}</strong>. The change only completes once that address is
          confirmed.</p>
        <p style="margin:0;font-size:13px;color:#64748b">
          🔒 If this wasn't you, change your password immediately — your account may be compromised.</p>"""
    return send_email(to_old, subject, _shell(app, "Email change requested", body), text)


def send_verification_email(to: str, verify_link: str, display_name: str | None) -> bool:
    """Compose + send the email-verification email. Safe to call from a
    BackgroundTask. Returns the underlying send result."""
    s = get_settings()
    app = s.email_from_name
    name = (display_name or "").strip() or "there"
    subject = f"Confirm your {app} email"
    text = (
        f"Hi {name},\n\n"
        f"Welcome to {app}! Please confirm this email address to finish setting "
        "up your copy-trading account.\n\n"
        f"Confirm your email here:\n{verify_link}\n\n"
        "If you didn't create this account, you can safely ignore this email.\n\n"
        f"— The {app} team\n"
    )
    html = f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 12px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <tr><td align="center">
    <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,0.08)">
      <tr><td style="background:#0f172a;padding:22px 32px">
        <span style="color:#ffffff;font-size:20px;font-weight:800;letter-spacing:0.5px">{app}</span>
        <span style="color:#60a5fa;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-left:8px">Copy trading</span>
      </td></tr>
      <tr><td style="padding:32px 32px 8px">
        <h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a">Confirm your email</h1>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">Hi {name},</p>
        <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">
          Welcome to <strong>{app}</strong>! Confirm this email address to finish
          setting up your copy-trading account and secure it.</p>
        <p style="margin:28px 0">
          <a href="{verify_link}"
             style="background:#2563eb;color:#ffffff;text-decoration:none;padding:13px 26px;border-radius:8px;font-weight:600;font-size:15px;display:inline-block">
            Confirm email
          </a>
        </p>
        <p style="margin:0 0 8px;font-size:13px;color:#64748b">If the button doesn't work, paste this link into your browser:</p>
        <p style="margin:0 0 8px;font-size:13px"><a href="{verify_link}" style="color:#2563eb;word-break:break-all">{verify_link}</a></p>
      </td></tr>
      <tr><td style="padding:8px 32px 28px">
        <div style="border-top:1px solid #e2e8f0;padding-top:16px">
          <p style="margin:0;font-size:13px;line-height:1.6;color:#64748b">
            If you didn't create a {app} account, you can safely ignore this email.</p>
        </div>
      </td></tr>
      <tr><td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0">
        <p style="margin:0;font-size:12px;color:#94a3b8">{app} · Automated copy trading</p>
      </td></tr>
    </table>
  </td></tr>
</table>"""
    template_id = s.sendgrid_verification_template_id or None
    dynamic_data = {"verify_link": verify_link, "name": name, "app_name": app}
    return send_email(
        to, subject, html, text, template_id=template_id, dynamic_data=dynamic_data
    )
