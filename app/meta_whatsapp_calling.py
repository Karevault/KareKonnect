"""Meta WhatsApp Cloud API Calling — webhook handling and optional business-initiated calls."""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

import requests

from app.integration_config import EffectiveIntegration, get_effective_integration
from app.meta_whatsapp import _graph_version, is_configured

_log = logging.getLogger(__name__)


def is_calling_enabled(cfg: Optional[EffectiveIntegration] = None) -> bool:
    c = cfg or get_effective_integration()
    return bool(is_configured() and c.meta_wa_calling_enabled)


def _phone_digits(phone_e164: str) -> str:
    return "".join(ch for ch in (phone_e164 or "") if ch.isdigit())


def iter_call_webhook_events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield normalized call events from Meta webhook payload (field=calls)."""
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "calls":
                continue
            value = change.get("value") or {}
            for call in value.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                yield {
                    "call_id": call.get("id") or call.get("call_id"),
                    "event": call.get("event") or call.get("status"),
                    "direction": call.get("direction"),
                    "from_digits": _phone_digits(str(call.get("from") or "")),
                    "to_digits": _phone_digits(str(call.get("to") or "")),
                    "session_id": (call.get("biz_opaque_callback_data") or "").strip() or None,
                    "raw": call,
                }


def _fetch_sdp_offer_from_bridge(*, session_id: str, to_digits: str) -> tuple[Optional[str], Optional[str]]:
    cfg = get_effective_integration()
    url = (cfg.meta_wa_calling_webrtc_bridge_url or "").strip()
    if not url:
        return None, "WebRTC bridge URL not configured"
    try:
        resp = requests.post(
            url,
            json={"session_id": session_id, "to": to_digits},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return None, str(exc)
    if not (200 <= resp.status_code < 300):
        return None, f"Bridge HTTP {resp.status_code}: {(resp.text or '')[:300]}"
    try:
        data = resp.json()
    except ValueError:
        return None, "Bridge returned invalid JSON"
    sdp = data.get("sdp") or data.get("offer")
    if not sdp:
        return None, "Bridge response missing sdp/offer"
    return str(sdp), None


def initiate_business_call(
    *,
    to_phone_e164: str,
    session_id: str,
    sdp_offer: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    POST /{phone_number_id}/calls with action=connect.
    Returns (ok, call_id, error). Requires WebRTC SDP unless bridge URL is set.
    """
    if not is_calling_enabled():
        return False, None, "Meta WhatsApp calling is not enabled"

    cfg = get_effective_integration()
    pid = (cfg.meta_wa_phone_number_id or "").strip()
    tok = (cfg.meta_wa_access_token or "").strip()
    if not pid or not tok:
        return False, None, "Meta WhatsApp not configured"

    to_digits = _phone_digits(to_phone_e164)
    if not to_digits:
        return False, None, "Invalid patient phone"

    offer = (sdp_offer or "").strip()
    if not offer:
        offer, bridge_err = _fetch_sdp_offer_from_bridge(session_id=session_id, to_digits=to_digits)
        if not offer:
            return (
                False,
                None,
                bridge_err
                or "WebRTC SDP offer required — set Meta calling bridge URL or use patient-initiated video in chat",
            )

    url = f"https://graph.facebook.com/{_graph_version(cfg)}/{pid}/calls"
    body: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "action": "connect",
        "session": {"sdp_type": "offer", "sdp": offer},
        "biz_opaque_callback_data": session_id[:512],
    }
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        _log.warning("Meta call connect failed: %s", exc)
        return False, None, str(exc)

    try:
        data = resp.json()
    except ValueError:
        return False, None, f"Meta HTTP {resp.status_code}: invalid JSON"

    if not (200 <= resp.status_code < 300):
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else (resp.text or "")[:300]
        code = err.get("code") if isinstance(err, dict) else None
        detail = f"Meta HTTP {resp.status_code}: {msg}"
        if code == 138006:
            detail += " (patient has not granted call permission — they can tap Video in this chat instead)"
        return False, None, detail

    call_id = None
    if isinstance(data, dict):
        call_id = data.get("call_id") or data.get("id")
        calls = data.get("calls")
        if not call_id and isinstance(calls, list) and calls:
            first = calls[0]
            if isinstance(first, dict):
                call_id = first.get("id") or first.get("call_id")
    return True, str(call_id) if call_id else None, None


def terminate_call(*, call_id: str) -> tuple[bool, Optional[str]]:
    if not is_calling_enabled():
        return False, "Meta WhatsApp calling is not enabled"
    cfg = get_effective_integration()
    pid = (cfg.meta_wa_phone_number_id or "").strip()
    tok = (cfg.meta_wa_access_token or "").strip()
    if not pid or not tok:
        return False, "Meta WhatsApp not configured"
    url = f"https://graph.facebook.com/{_graph_version(cfg)}/{pid}/calls"
    body = {"messaging_product": "whatsapp", "call_id": call_id, "action": "terminate"}
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    if 200 <= resp.status_code < 300:
        return True, None
    return False, (resp.text or "")[:300]
