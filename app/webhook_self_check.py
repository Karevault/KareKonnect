from __future__ import annotations

from typing import List, Tuple
from urllib.parse import urlencode

import requests

from app import meta_whatsapp as meta_wa
from app.integration_config import get_effective_integration


def _base() -> str:
    return get_effective_integration().hc_base_url.rstrip("/")


def test_meta_webhook_verify() -> Tuple[bool, List[str]]:
    """GET hub challenge against our Meta verify endpoint."""
    cfg = get_effective_integration()
    vt = (cfg.meta_wa_verify_token or "").strip()
    if not vt:
        return False, ["META_WA_VERIFY_TOKEN is not set — cannot test verify URL."]
    if not meta_wa.verify_token_configured():
        return False, ["Verify token not configured."]
    qs = urlencode(
        {"hub.mode": "subscribe", "hub.verify_token": vt, "hub.challenge": "healthconnect_selftest_ok"}
    )
    url = f"{_base()}/webhooks/meta/whatsapp?{qs}"
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        return False, [f"Request failed: {e}"]
    body = (r.text or "").strip()
    if r.status_code == 200 and body == "healthconnect_selftest_ok":
        return True, ["GET verify OK (HTTP 200, challenge echoed).", f"URL used: {url}"]
    return False, [f"HTTP {r.status_code}", body[:800]]


def test_twilio_status_webhook() -> Tuple[bool, List[str]]:
    """POST a sample Twilio status payload to our status URL."""
    url = f"{_base()}/webhooks/twilio/status"
    data = {
        "MessageSid": "SM_healthconnect_selftest",
        "MessageStatus": "delivered",
        "To": "+15551234567",
        "From": "+15559876543",
        "ErrorCode": "0",
    }
    try:
        r = requests.post(url, data=data, timeout=15)
    except requests.RequestException as e:
        return False, [f"Request failed: {e}"]
    if r.status_code == 200 and (r.text or "").strip().upper() == "OK":
        return True, [
            "POST status sample OK.",
            "Open Twilio delivery events in admin — you should see SM_healthconnect_selftest.",
        ]
    return False, [f"HTTP {r.status_code}", (r.text or "")[:800]]


def test_twilio_inbound_webhook() -> Tuple[bool, List[str]]:
    """POST a minimal inbound INFO to Twilio inbound (no carrier involved)."""
    url = f"{_base()}/webhooks/twilio/inbound"
    data = {
        "MessageSid": "SM_healthconnect_inbound_test",
        "From": "+15551234567",
        "To": "+15559876543",
        "Body": "INFO",
    }
    try:
        r = requests.post(url, data=data, timeout=15)
    except requests.RequestException as e:
        return False, [f"Request failed: {e}"]
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code == 200 and "xml" in ct:
        return True, ["POST inbound INFO OK — TwiML returned.", "Check Inbound messages in admin."]
    return False, [f"HTTP {r.status_code}", (r.text or "")[:800]]
