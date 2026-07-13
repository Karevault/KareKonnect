"""WhatsApp voice notes: inbound STT and optional outbound TTS."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app import google_voice as gv
from app import meta_whatsapp as meta_wa
from app.config import settings
from app.database import session_scope
from app.messaging import SendResult, send_message
from app.models import VoiceMessageLog
from app.services import get_app_settings, get_user_language
from app.util import new_id, now_iso

_log = logging.getLogger(__name__)


def voice_features_configured() -> bool:
    return meta_wa.is_configured() and gv.is_stt_configured()


def inbound_voice_enabled() -> bool:
    if not voice_features_configured():
        return False
    try:
        cfg = get_app_settings()
        return bool(cfg.whatsapp_voice_inbound_enabled)
    except Exception:
        return settings.whatsapp_voice_inbound_enabled


def outbound_voice_enabled() -> bool:
    if not voice_features_configured() or not gv.is_tts_configured():
        return False
    try:
        cfg = get_app_settings()
        return bool(cfg.whatsapp_voice_outbound_enabled)
    except Exception:
        return settings.whatsapp_voice_outbound_enabled


def outbound_voice_max_chars() -> int:
    try:
        cfg = get_app_settings()
        n = int(cfg.whatsapp_voice_outbound_max_chars or 0)
        return n if n > 0 else settings.whatsapp_voice_outbound_max_chars
    except Exception:
        return settings.whatsapp_voice_outbound_max_chars


def user_wants_voice_replies(phone_e164: str) -> bool:
    from sqlalchemy import select

    from app.models import User
    from app.util import normalize_phone_e164

    phone = normalize_phone_e164(phone_e164)
    try:
        with session_scope() as s:
            row = s.execute(
                select(User.whatsapp_voice_replies_opt_in).where(User.phone_e164 == phone)
            ).scalar_one_or_none()
        return bool(row)
    except Exception:
        _log.exception("whatsapp_voice_replies_opt_in lookup failed for %s", phone)
        return False


def set_whatsapp_voice_replies_opt_in(*, phone_e164: str, enabled: bool) -> None:
    from sqlalchemy import select, update

    from app.models import User
    from app.util import normalize_phone_e164

    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if user:
            s.execute(
                update(User)
                .where(User.phone_e164 == phone)
                .values(whatsapp_voice_replies_opt_in=enabled, updated_at=now)
            )


def log_voice_event(
    *,
    direction: str,
    phone_e164: str,
    text: Optional[str],
    success: bool,
    error: Optional[str] = None,
    provider: str = "google",
    media_id: Optional[str] = None,
    meta_message_id: Optional[str] = None,
) -> None:
    try:
        with session_scope() as s:
            s.add(
                VoiceMessageLog(
                    id=new_id("vml"),
                    direction=direction,
                    phone_e164=phone_e164,
                    media_id=media_id,
                    meta_message_id=meta_message_id,
                    text_preview=(text or "")[:2000] if text else None,
                    success=success,
                    error=(error or "")[:500] if error else None,
                    provider=provider,
                    created_at=now_iso(),
                )
            )
    except Exception:
        _log.exception("voice log write failed")


def transcribe_inbound_audio(
    *,
    media_id: str,
    phone_e164: str,
    meta_message_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Download WhatsApp audio from Meta and run STT.
    Returns (transcript, error).
    """
    if not inbound_voice_enabled():
        return None, "inbound_voice_disabled"

    audio_bytes, mime_type, dl_err = meta_wa.download_media(media_id)
    if dl_err or not audio_bytes:
        log_voice_event(
            direction="inbound",
            phone_e164=phone_e164,
            text=None,
            success=False,
            error=dl_err or "download_failed",
            media_id=media_id,
            meta_message_id=meta_message_id,
        )
        return None, dl_err or "download_failed"

    lang = get_user_language(phone_e164)
    transcript, stt_err = gv.speech_to_text(audio_bytes, user_lang=lang, mime_type=mime_type)
    log_voice_event(
        direction="inbound",
        phone_e164=phone_e164,
        text=transcript,
        success=transcript is not None,
        error=stt_err,
        media_id=media_id,
        meta_message_id=meta_message_id,
    )
    return transcript, stt_err


def send_whatsapp_patient_reply(
    *,
    to_phone_e164: str,
    body: str,
    patient_phone_e164: Optional[str] = None,
    allow_voice: bool = True,
) -> SendResult:
    """
    Send WhatsApp reply: optional voice note when patient opted in and message is short enough;
    always falls back to text on failure.

    Meta webhooks must respond quickly — use allow_voice=False from the webhook handler so
    replies go out as text immediately (TTS + media upload can exceed Meta's timeout).
    """
    text = (body or "").strip()
    if not text:
        return SendResult(ok=True)

    phone = patient_phone_e164 or to_phone_e164
    try:
        use_voice = (
            allow_voice
            and outbound_voice_enabled()
            and user_wants_voice_replies(phone)
            and len(text) <= outbound_voice_max_chars()
        )

        if use_voice:
            lang = get_user_language(phone)
            audio_bytes, tts_err = gv.text_to_speech_ogg(text, user_lang=lang)
            if audio_bytes and not tts_err:
                ok, err = meta_wa.send_audio_message(
                    to_phone_e164=to_phone_e164,
                    audio_bytes=audio_bytes,
                    mime_type="audio/ogg",
                )
                log_voice_event(
                    direction="outbound",
                    phone_e164=phone,
                    text=text,
                    success=ok,
                    error=err,
                    provider="google+meta",
                )
                if ok:
                    return SendResult(ok=True)
                _log.warning("WhatsApp audio send failed, falling back to text: %s", err)

        res = send_message(to_phone_e164=to_phone_e164, body=text, channel="whatsapp")
        if not res.ok:
            log_voice_event(
                direction="outbound",
                phone_e164=phone,
                text=text,
                success=False,
                error=res.error,
                provider="text",
            )
        return res
    except Exception as e:
        _log.exception("send_whatsapp_patient_reply failed, trying plain text")
        try:
            return send_message(to_phone_e164=to_phone_e164, body=text, channel="whatsapp")
        except Exception:
            return SendResult(ok=False, error=str(e))


def voice_status() -> dict[str, Any]:
    try:
        cfg = get_app_settings()
        inbound_on = bool(cfg.whatsapp_voice_inbound_enabled)
        outbound_on = bool(cfg.whatsapp_voice_outbound_enabled)
        max_chars = int(cfg.whatsapp_voice_outbound_max_chars)
    except Exception:
        inbound_on = settings.whatsapp_voice_inbound_enabled
        outbound_on = settings.whatsapp_voice_outbound_enabled
        max_chars = settings.whatsapp_voice_outbound_max_chars

    return {
        "meta_whatsapp_configured": meta_wa.is_configured(),
        "google_stt_configured": gv.is_stt_configured(),
        "google_tts_configured": gv.is_tts_configured(),
        "inbound_voice_enabled": inbound_on and inbound_voice_enabled(),
        "outbound_voice_enabled": outbound_on and outbound_voice_enabled(),
        "outbound_max_chars": max_chars,
        "patient_opt_in_keyword": "VOICE YES",
        "patient_opt_out_keyword": "VOICE NO",
    }
