from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Iterator, Optional, Tuple

import requests

from app.integration_config import EffectiveIntegration, get_effective_integration

_log = logging.getLogger(__name__)

# Cache Graph lookup for wa.me (phone_number_id + token hash -> digits).
_wa_me_cache: Optional[Tuple[float, str, str, Optional[str]]] = None
_WA_ME_CACHE_TTL_SEC = 3600.0


def is_configured() -> bool:
    cfg = get_effective_integration()
    return bool(
        (cfg.meta_wa_phone_number_id or "").strip() and (cfg.meta_wa_access_token or "").strip()
    )


def verify_token_configured() -> bool:
    cfg = get_effective_integration()
    return bool((cfg.meta_wa_verify_token or "").strip())


def verify_webhook_subscription(*, mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Optional[str]:
    """
    Meta sends GET hub.mode=subscribe, hub.verify_token, hub.challenge.
    Return challenge text to echo on success, else None.
    """
    if mode != "subscribe" or not challenge:
        return None
    cfg = get_effective_integration()
    expected = (cfg.meta_wa_verify_token or "").strip()
    if not expected or token != expected:
        return None
    return challenge


def verify_signature_sha256(body: bytes, x_hub_signature_256: Optional[str]) -> bool:
    cfg = get_effective_integration()
    secret = (cfg.meta_app_secret or "").strip()
    if not secret:
        return True
    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        return False
    expected_hex = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    received_hex = x_hub_signature_256.split("=", 1)[1].strip()
    return hmac.compare_digest(expected_hex, received_hex)


def _graph_version(cfg: Optional[EffectiveIntegration] = None) -> str:
    c = cfg or get_effective_integration()
    v = (c.meta_graph_version or "v21.0").strip()
    if not v.startswith("v"):
        return f"v{v}"
    return v


def digits_from_display_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    return digits or None


def get_wa_me_digits_from_meta(*, cfg: Optional[EffectiveIntegration] = None) -> Optional[str]:
    """
    WhatsApp Cloud API phone_number_id is not the business MSISDN.
    Resolve display_phone_number from Graph for wa.me links when HC_WHATSAPP_NUMBER is unset.
    """
    global _wa_me_cache
    c = cfg or get_effective_integration()
    pid = (c.meta_wa_phone_number_id or "").strip()
    tok = (c.meta_wa_access_token or "").strip()
    if not pid or not tok:
        return None
    tok_h = hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]
    now = time.monotonic()
    if _wa_me_cache is not None:
        ts, c_pid, c_tok_h, digits = _wa_me_cache
        if c_pid == pid and c_tok_h == tok_h and (now - ts) < _WA_ME_CACHE_TTL_SEC:
            return digits

    url = f"https://graph.facebook.com/{_graph_version(c)}/{pid}"
    params = {"fields": "display_phone_number"}
    headers = {"Authorization": f"Bearer {tok}"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.RequestException as e:
        _log.warning("Meta display_phone_number fetch failed: %s", e)
        _wa_me_cache = (now, pid, tok_h, None)
        return None
    if not (200 <= resp.status_code < 300):
        _log.warning(
            "Meta display_phone_number fetch HTTP %s: %s",
            resp.status_code,
            (resp.text or "")[:500],
        )
        _wa_me_cache = (now, pid, tok_h, None)
        return None
    data = resp.json() or {}
    display = data.get("display_phone_number")
    digits = digits_from_display_phone(str(display) if display is not None else "")
    _wa_me_cache = (now, pid, tok_h, digits)
    return digits


def send_text_message(*, to_phone_e164: str, body: str) -> tuple[bool, Optional[str]]:
    """
    Send a WhatsApp text message via Cloud API.
    `to` must be digits only (country code included, no +).
    """
    cfg = get_effective_integration()
    if not (cfg.meta_wa_phone_number_id or "").strip() or not (cfg.meta_wa_access_token or "").strip():
        return False, "meta_whatsapp_not_configured"
    digits = "".join(c for c in to_phone_e164 if c.isdigit())
    if not digits:
        return False, "invalid_recipient"

    url = (
        f"https://graph.facebook.com/{_graph_version(cfg)}/"
        f"{cfg.meta_wa_phone_number_id.strip()}/messages"
    )
    headers = {
        "Authorization": f"Bearer {cfg.meta_wa_access_token.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": digits,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
    except requests.RequestException as e:
        return False, f"meta_request_error:{e}"

    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"meta_http_{resp.status_code}:{resp.text[:800]}"


def _inbound_user_text_from_message(msg: dict[str, Any]) -> Optional[str]:
    """
    Extract user-visible text for routing (JOIN, INFO, MENU, etc.).
    Cloud API sends type=text for normal messages; interactive/button for templates & quick actions.
    """
    mtype = (msg.get("type") or "").strip().lower()
    if mtype == "text":
        text_obj = msg.get("text") or {}
        body = (text_obj.get("body") or "").strip()
        return body or None
    if mtype == "button":
        btn = msg.get("button") or {}
        t = (btn.get("text") or btn.get("payload") or "").strip()
        return t or None
    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        br = inter.get("button_reply") or {}
        title = (br.get("title") or "").strip()
        if title:
            return title
        lr = inter.get("list_reply") or {}
        lt = (lr.get("title") or "").strip()
        if lt:
            return lt
        lid = (lr.get("id") or "").strip()
        return lid or None
    return None


def download_media(media_id: str) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Resolve Meta media ID and download bytes.
    Returns (bytes, mime_type, error).
    """
    cfg = get_effective_integration()
    mid = (media_id or "").strip()
    if not mid:
        return None, None, "missing_media_id"
    if not (cfg.meta_wa_access_token or "").strip():
        return None, None, "meta_whatsapp_not_configured"

    headers = {"Authorization": f"Bearer {cfg.meta_wa_access_token.strip()}"}
    meta_url = f"https://graph.facebook.com/{_graph_version(cfg)}/{mid}"
    try:
        meta_resp = requests.get(meta_url, headers=headers, timeout=20)
    except requests.RequestException as e:
        return None, None, f"meta_media_meta_error:{e}"
    if not (200 <= meta_resp.status_code < 300):
        return None, None, f"meta_media_meta_http_{meta_resp.status_code}:{meta_resp.text[:400]}"

    meta = meta_resp.json() or {}
    download_url = meta.get("url")
    mime_type = meta.get("mime_type")
    if not download_url:
        return None, None, "meta_media_no_url"

    try:
        dl_resp = requests.get(str(download_url), headers=headers, timeout=60)
    except requests.RequestException as e:
        return None, None, f"meta_media_download_error:{e}"
    if not (200 <= dl_resp.status_code < 300):
        return None, None, f"meta_media_download_http_{dl_resp.status_code}"

    return dl_resp.content, str(mime_type) if mime_type else None, None


def upload_media(*, audio_bytes: bytes, mime_type: str = "audio/ogg") -> tuple[Optional[str], Optional[str]]:
    """Upload audio for outbound WhatsApp. Returns (media_id, error)."""
    cfg = get_effective_integration()
    pid = (cfg.meta_wa_phone_number_id or "").strip()
    tok = (cfg.meta_wa_access_token or "").strip()
    if not pid or not tok:
        return None, "meta_whatsapp_not_configured"
    if not audio_bytes:
        return None, "empty_audio"

    url = f"https://graph.facebook.com/{_graph_version(cfg)}/{pid}/media"
    headers = {"Authorization": f"Bearer {tok}"}
    files = {"file": ("voice.ogg", audio_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": "audio"}
    try:
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=45)
    except requests.RequestException as e:
        return None, f"meta_upload_error:{e}"
    if not (200 <= resp.status_code < 300):
        return None, f"meta_upload_http_{resp.status_code}:{resp.text[:500]}"
    media_id = (resp.json() or {}).get("id")
    if not media_id:
        return None, "meta_upload_no_id"
    return str(media_id), None


def send_audio_message(
    *,
    to_phone_e164: str,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> tuple[bool, Optional[str]]:
    """Send WhatsApp audio message (upload then send)."""
    media_id, up_err = upload_media(audio_bytes=audio_bytes, mime_type=mime_type)
    if up_err or not media_id:
        return False, up_err or "upload_failed"

    cfg = get_effective_integration()
    digits = "".join(c for c in to_phone_e164 if c.isdigit())
    if not digits:
        return False, "invalid_recipient"

    url = (
        f"https://graph.facebook.com/{_graph_version(cfg)}/"
        f"{cfg.meta_wa_phone_number_id.strip()}/messages"
    )
    headers = {
        "Authorization": f"Bearer {cfg.meta_wa_access_token.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": digits,
        "type": "audio",
        "audio": {"id": media_id},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
    except requests.RequestException as e:
        return False, f"meta_request_error:{e}"
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"meta_http_{resp.status_code}:{resp.text[:800]}"


def _template_language_candidates(language_code: str) -> list[str]:
    """Meta often registers English templates as en_US, not en."""
    lang = (language_code or "en_US").strip().replace("-", "_")
    candidates = [lang]
    aliases = {
        "en": ["en_US", "en_GB"],
        "english": ["en_US", "en_GB"],
        "en_us": ["en_US"],
    }
    for alt in aliases.get(lang.lower(), []):
        if alt not in candidates:
            candidates.append(alt)
    if lang != "en_US" and "en_US" not in candidates:
        candidates.append("en_US")
    return candidates


def list_message_templates(*, waba_id: str, limit: int = 50) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return approved templates for a WhatsApp Business Account ID."""
    cfg = get_effective_integration()
    wid = (waba_id or "").strip()
    if not wid:
        return [], "missing_waba_id"
    if not (cfg.meta_wa_access_token or "").strip():
        return [], "meta_whatsapp_not_configured"
    url = f"https://graph.facebook.com/{_graph_version(cfg)}/{wid}/message_templates"
    headers = {"Authorization": f"Bearer {cfg.meta_wa_access_token.strip()}"}
    try:
        resp = requests.get(url, headers=headers, params={"limit": limit}, timeout=25)
    except requests.RequestException as e:
        return [], f"meta_request_error:{e}"
    if not (200 <= resp.status_code < 300):
        return [], f"meta_http_{resp.status_code}:{resp.text[:500]}"
    return list((resp.json() or {}).get("data") or []), None


def _send_template_message_once(
    *,
    to_phone_e164: str,
    template_name: str,
    language_code: str,
    body_parameters: Optional[list[str]] = None,
) -> tuple[bool, Optional[str]]:
    cfg = get_effective_integration()
    if not (cfg.meta_wa_phone_number_id or "").strip() or not (cfg.meta_wa_access_token or "").strip():
        return False, "meta_whatsapp_not_configured"
    name = (template_name or "").strip()
    if not name:
        return False, "meta_template_name_missing"
    digits = "".join(c for c in to_phone_e164 if c.isdigit())
    if not digits:
        return False, "invalid_recipient"

    lang = (language_code or "en_US").strip().replace("-", "_")
    components: list[dict[str, Any]] = []
    params = [(p or "").strip() for p in (body_parameters or []) if (p or "").strip()]
    if params:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p[:1024]} for p in params],
            }
        )

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": digits,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
        },
    }
    if components:
        payload["template"]["components"] = components

    url = (
        f"https://graph.facebook.com/{_graph_version(cfg)}/"
        f"{cfg.meta_wa_phone_number_id.strip()}/messages"
    )
    headers = {
        "Authorization": f"Bearer {cfg.meta_wa_access_token.strip()}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
    except requests.RequestException as e:
        return False, f"meta_request_error:{e}"

    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"meta_http_{resp.status_code}:{resp.text[:800]}"


def send_template_message(
    *,
    to_phone_e164: str,
    template_name: str,
    language_code: str = "en_US",
    body_parameters: Optional[list[str]] = None,
) -> tuple[bool, Optional[str]]:
    """
    Send an approved WhatsApp template (required outside the 24h customer-service window).
    body_parameters fill {{1}}, {{2}}, … in the template BODY component.
    Tries en_US when en is configured (Meta's usual English locale).
    """
    last_err: Optional[str] = None
    for lang in _template_language_candidates(language_code):
        ok, err = _send_template_message_once(
            to_phone_e164=to_phone_e164,
            template_name=template_name,
            language_code=lang,
            body_parameters=body_parameters,
        )
        if ok:
            return True, None
        last_err = err
        if err and "132001" not in err:
            return False, err
    hint = (
        f"Template {template_name!r} not found for language {language_code!r} "
        f"(tried {', '.join(_template_language_candidates(language_code))}). "
        "Confirm the exact template name and language in WhatsApp Manager for this phone number."
    )
    return False, last_err or hint


def iter_message_statuses(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield Meta delivery/read/failed status updates from webhook payloads."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for st in value.get("statuses") or []:
                recipient = str(st.get("recipient_id") or "").strip()
                status = str(st.get("status") or "").strip().lower()
                if not recipient or not status:
                    continue
                errors = st.get("errors") or []
                err_bits: list[str] = []
                for err in errors:
                    if not isinstance(err, dict):
                        continue
                    code = err.get("code")
                    title = err.get("title") or err.get("message")
                    if code or title:
                        err_bits.append(f"{code}:{title}" if code else str(title))
                yield {
                    "message_id": str(st.get("id") or ""),
                    "status": status,
                    "recipient_id": recipient,
                    "error": "; ".join(err_bits) or None,
                }


def iter_inbound_media_messages(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield image, document, and video messages with media_id and optional caption."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            display_phone = metadata.get("display_phone_number")
            for msg in value.get("messages") or []:
                mtype = (msg.get("type") or "").strip().lower()
                if mtype not in ("image", "document", "video"):
                    continue
                block = msg.get(mtype) or {}
                media_id = str(block.get("id") or "").strip()
                if not media_id:
                    continue
                from_id = str(msg.get("from") or "").strip()
                if not from_id:
                    continue
                mid = str(msg.get("id") or "")
                caption = str(block.get("caption") or "").strip() or None
                filename = str(block.get("filename") or "").strip() or None
                yield {
                    "from_wa_id": from_id,
                    "to_display_phone": str(display_phone) if display_phone else None,
                    "media_type": mtype,
                    "media_id": media_id,
                    "mime_type": str(block.get("mime_type") or ""),
                    "caption": caption,
                    "filename": filename,
                    "message_id": mid,
                    "raw": {
                        "source": "meta_whatsapp",
                        "message_id": mid,
                        "wa_message_type": mtype,
                        "media_id": media_id,
                    },
                }


def send_image_message(
    *,
    to_phone_e164: str,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    caption: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Upload image and send to patient (e.g. provider-shared resource)."""
    cfg = get_effective_integration()
    pid = (cfg.meta_wa_phone_number_id or "").strip()
    tok = (cfg.meta_wa_access_token or "").strip()
    if not pid or not tok or not image_bytes:
        return False, "meta_whatsapp_not_configured"
    url = f"https://graph.facebook.com/{_graph_version(cfg)}/{pid}/media"
    headers = {"Authorization": f"Bearer {tok}"}
    ext = "jpg" if "jpeg" in mime_type else "png"
    files = {"file": (f"image.{ext}", image_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": "image"}
    try:
        up = requests.post(url, headers=headers, data=data, files=files, timeout=45)
    except requests.RequestException as e:
        return False, str(e)
    if not (200 <= up.status_code < 300):
        return False, f"meta_upload_http_{up.status_code}"
    media_id = (up.json() or {}).get("id")
    if not media_id:
        return False, "meta_upload_no_id"
    digits = "".join(c for c in to_phone_e164 if c.isdigit())
    msg_url = f"https://graph.facebook.com/{_graph_version(cfg)}/{pid}/messages"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": digits,
        "type": "image",
        "image": {"id": str(media_id)},
    }
    if caption:
        payload["image"]["caption"] = caption[:1024]
    try:
        resp = requests.post(msg_url, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=25)
    except requests.RequestException as e:
        return False, str(e)
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"meta_http_{resp.status_code}:{resp.text[:500]}"


def iter_inbound_audio_messages(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield voice note events: from_wa_id, media_id, message_id, mime_type, raw."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            display_phone = metadata.get("display_phone_number")
            for msg in value.get("messages") or []:
                if (msg.get("type") or "").strip().lower() != "audio":
                    continue
                audio = msg.get("audio") or {}
                media_id = str(audio.get("id") or "").strip()
                if not media_id:
                    continue
                from_id = str(msg.get("from") or "").strip()
                if not from_id:
                    continue
                mid = str(msg.get("id") or "")
                yield {
                    "from_wa_id": from_id,
                    "to_display_phone": str(display_phone) if display_phone else None,
                    "media_id": media_id,
                    "mime_type": str(audio.get("mime_type") or ""),
                    "message_id": mid,
                    "raw": {
                        "source": "meta_whatsapp",
                        "message_id": mid,
                        "phone_number_id": str(metadata.get("phone_number_id") or ""),
                        "display_phone_number": str(display_phone or ""),
                        "wa_message_type": "audio",
                        "media_id": media_id,
                    },
                }


def iter_inbound_text_messages(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """
    Yield dicts: from_wa_id (digits), to_display_phone (optional str), body (str), message_id (str), raw (dict).
    """
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            display_phone = metadata.get("display_phone_number")
            for msg in value.get("messages") or []:
                body = _inbound_user_text_from_message(msg)
                if not body:
                    continue
                from_id = str(msg.get("from") or "").strip()
                if not from_id:
                    continue
                mid = str(msg.get("id") or "")
                raw = {
                    "source": "meta_whatsapp",
                    "message_id": mid,
                    "phone_number_id": str(metadata.get("phone_number_id") or ""),
                    "display_phone_number": str(display_phone or ""),
                    "wa_message_type": str(msg.get("type") or ""),
                }
                yield {
                    "from_wa_id": from_id,
                    "to_display_phone": str(display_phone) if display_phone else None,
                    "body": body,
                    "message_id": mid,
                    "raw": raw,
                }
