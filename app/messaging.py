from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal, Optional

import requests

from app.africastalking import at_sms_ready, send_sms as at_send_sms
from app import meta_whatsapp as meta_wa
from app.integration_config import get_effective_integration


Channel = Literal["sms", "whatsapp"]


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: Optional[str] = None
    via: Optional[str] = None  # e.g. whatsapp_text, whatsapp_template, sms_meta


def _twilio_ready() -> bool:
    e = get_effective_integration()
    return bool(e.twilio_account_sid and e.twilio_auth_token and e.twilio_messaging_service_sid)


def send_message(*, to_phone_e164: str, body: str, channel: Channel) -> SendResult:
    """
    Outbound SMS: Africa's Talking when AT_* env is set, otherwise Twilio.
    Outbound WhatsApp: Meta Cloud API when META_WA_* is set, otherwise Twilio Messaging Service.
    Returns ok=False when the channel is not configured (no silent no-op).
    """
    if channel == "sms" and at_sms_ready():
        r = at_send_sms(to_e164=to_phone_e164, message=body)
        return SendResult(ok=r.ok, error=r.error)

    if channel == "whatsapp" and meta_wa.is_configured():
        ok, err = meta_wa.send_text_message(to_phone_e164=to_phone_e164, body=body)
        return SendResult(ok=ok, error=err, via="whatsapp_text")

    e = get_effective_integration()
    if not _twilio_ready():
        return SendResult(ok=False, error=f"{channel}_not_configured")

    to = to_phone_e164
    if channel == "whatsapp":
        to = f"whatsapp:{to_phone_e164}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{e.twilio_account_sid}/Messages.json"
    auth = f"{e.twilio_account_sid}:{e.twilio_auth_token}".encode("utf-8")
    headers = {
        "Authorization": f"Basic {base64.b64encode(auth).decode('ascii')}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "To": to,
        "MessagingServiceSid": e.twilio_messaging_service_sid,
        "Body": body,
    }
    base = (e.hc_base_url or "").rstrip("/")
    if base.startswith("https://"):
        data["StatusCallback"] = f"{base}/webhooks/twilio/status"
    resp = requests.post(url, data=data, headers=headers, timeout=20)
    if 200 <= resp.status_code < 300:
        return SendResult(ok=True, via="twilio")
    return SendResult(ok=False, error=f"twilio_http_{resp.status_code}:{resp.text[:500]}")


def send_whatsapp_broadcast(*, to_phone_e164: str, body: str) -> SendResult:
    """
    WhatsApp broadcast: free-form text inside the 24h session window;
    approved template outside the window (when configured).
    """
    from app.whatsapp_session import whatsapp_session_open

    text = (body or "").strip()
    if not text:
        return SendResult(ok=False, error="empty_body")

    if not meta_wa.is_configured():
        return SendResult(ok=False, error="meta_whatsapp_not_configured")

    if whatsapp_session_open(to_phone_e164):
        ok, err = meta_wa.send_text_message(to_phone_e164=to_phone_e164, body=text)
        return SendResult(ok=ok, error=err, via="whatsapp_text")

    cfg = get_effective_integration()
    template = (cfg.meta_wa_broadcast_template or "").strip()
    if template:
        lang = (cfg.meta_wa_broadcast_template_language or "en_US").strip()
        ok, err = meta_wa.send_template_message(
            to_phone_e164=to_phone_e164,
            template_name=template,
            language_code=lang,
            body_parameters=[text],
        )
        return SendResult(ok=ok, error=err, via="whatsapp_template")

    return SendResult(
        ok=False,
        error=(
            "whatsapp_session_closed:131047 — recipient has not messaged in 24h. "
            "Set Meta broadcast template in Integration settings (utility template with {{1}} body)."
        ),
        via="whatsapp_blocked",
    )

