from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from app.integration_config import get_effective_integration


@dataclass(frozen=True)
class AtSendResult:
    ok: bool
    error: Optional[str] = None


def at_sms_ready() -> bool:
    x = get_effective_integration()
    return bool(x.at_username and x.at_api_key and x.at_shortcode)


def normalize_at_phone_number(value: str) -> str:
    """
    Africa's Talking often sends international numbers with or without '+'.
    """
    v = (value or "").strip().replace(" ", "")
    if v.lower().startswith("whatsapp:"):
        v = v.split(":", 1)[1]
    if v.startswith("+"):
        return v
    if v.startswith("00") and len(v) > 2:
        return "+" + v[2:]
    if v and v[0].isdigit():
        return "+" + v
    return v


def send_sms(*, to_e164: str, message: str) -> AtSendResult:
    """
    Outbound SMS via Africa's Talking HTTP API.
    https://developers.africastalking.com/docs/sms/sending (see official docs for your environment).
    """
    if not at_sms_ready():
        return AtSendResult(ok=False, error="africastalking_not_configured")

    x = get_effective_integration()
    to = normalize_at_phone_number(to_e164)
    url = "https://api.africastalking.com/version1/messaging"
    headers = {
        "Accept": "application/json",
        "apiKey": x.at_api_key or "",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "username": x.at_username or "",
        "to": to,
        "message": message,
        "from": x.at_shortcode or "",
    }
    try:
        resp = requests.post(url, headers=headers, data=data, timeout=25)
        if 200 <= resp.status_code < 300:
            return AtSendResult(ok=True)
        return AtSendResult(ok=False, error=f"at_http_{resp.status_code}:{resp.text[:400]}")
    except Exception as e:
        return AtSendResult(ok=False, error=str(e)[:400])


def parse_incoming_sms_form(form: dict[str, Any]) -> tuple[str, str, str, dict[str, str]]:
    """
    Two-way / inbound SMS callback (application/x-www-form-urlencoded).
    https://developers.africastalking.com/docs/sms/notifications
    """
    raw = {str(k): str(v) for k, v in form.items()}
    from_n = str(form.get("from") or form.get("fromNumber") or "")
    text = str(form.get("text") or form.get("message") or "").strip()
    to_n = str(form.get("to") or form.get("shortCode") or "")
    return from_n, to_n, text, raw


def parse_ussd_form(form: dict[str, Any]) -> tuple[str, str, str, str, dict[str, str]]:
    """
    USSD callback fields per Africa's Talking USSD docs.
    https://developers.africastalking.com/docs/ussd/overview
    """
    raw = {str(k): str(v) for k, v in form.items()}
    session_id = str(form.get("sessionId") or "")
    service_code = str(form.get("serviceCode") or "")
    phone = str(form.get("phoneNumber") or "")
    text = str(form.get("text") or "")
    return session_id, service_code, phone, text, raw
