"""Google Cloud Speech-to-Text and Text-to-Speech (REST, API key)."""

from __future__ import annotations

import base64
import logging
from typing import Optional, Tuple

import requests

from app.config import settings

_log = logging.getLogger(__name__)

_STT_URL = "https://speech.googleapis.com/v1/speech:recognize"
_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

# WhatsApp voice notes are typically OGG Opus.
_STT_ENCODING = "OGG_OPUS"
_STT_SAMPLE_RATE = 16000


def google_voice_api_key() -> Optional[str]:
    key = (settings.google_cloud_api_key or settings.google_translate_api_key or "").strip()
    return key or None


def is_stt_configured() -> bool:
    return bool(google_voice_api_key())


def is_tts_configured() -> bool:
    return bool(google_voice_api_key())


def _language_code_for_stt(user_lang: Optional[str]) -> str:
    m = {
        "en": "en-NG",
        "pcm": "en-NG",
        "ha": "ha-NG",
        "ig": "ig-NG",
        "yo": "yo-NG",
    }
    return m.get((user_lang or "en").lower(), "en-NG")


def _voice_for_tts(user_lang: Optional[str]) -> Tuple[str, str]:
    """Return (languageCode, voice name) for Neural2 where available."""
    lang = (user_lang or "en").lower()
    if lang == "ha":
        return "ha-NG", "ha-NG-Standard-A"
    if lang == "yo":
        return "yo-NG", "yo-NG-Standard-A"
    return "en-GB", "en-GB-Neural2-A"


def speech_to_text(
    audio_bytes: bytes,
    *,
    user_lang: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Transcribe audio bytes. Returns (transcript, error).
    """
    key = google_voice_api_key()
    if not key:
        return None, "google_voice_not_configured"
    if not audio_bytes:
        return None, "empty_audio"

    encoding = _STT_ENCODING
    sample_rate = _STT_SAMPLE_RATE
    if mime_type:
        mt = mime_type.lower()
        if "mpeg" in mt or "mp3" in mt:
            encoding = "MP3"
        elif "wav" in mt:
            encoding = "LINEAR16"
            sample_rate = 48000
        elif "aac" in mt or "m4a" in mt:
            encoding = "ENCODING_UNSPECIFIED"

    payload = {
        "config": {
            "encoding": encoding,
            "sampleRateHertz": sample_rate,
            "languageCode": _language_code_for_stt(user_lang),
            "enableAutomaticPunctuation": True,
            "model": "default",
        },
        "audio": {"content": base64.b64encode(audio_bytes).decode("ascii")},
    }
    try:
        resp = requests.post(
            _STT_URL,
            params={"key": key},
            json=payload,
            timeout=45,
        )
    except requests.RequestException as e:
        return None, f"stt_request_error:{e}"

    if not (200 <= resp.status_code < 300):
        return None, f"stt_http_{resp.status_code}:{(resp.text or '')[:500]}"

    data = resp.json() or {}
    results = data.get("results") or []
    parts: list[str] = []
    for r in results:
        for alt in r.get("alternatives") or []:
            t = (alt.get("transcript") or "").strip()
            if t:
                parts.append(t)
    transcript = " ".join(parts).strip()
    if not transcript:
        return None, "stt_empty_transcript"
    return transcript, None


def text_to_speech_ogg(
    text: str,
    *,
    user_lang: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Synthesize OGG Opus audio for WhatsApp. Returns (bytes, error)."""
    key = google_voice_api_key()
    if not key:
        return None, "google_voice_not_configured"
    body = (text or "").strip()
    if not body:
        return None, "empty_text"

    lang_code, voice_name = _voice_for_tts(user_lang)
    payload = {
        "input": {"text": body[:1500]},
        "voice": {"languageCode": lang_code, "name": voice_name},
        "audioConfig": {
            "audioEncoding": "OGG_OPUS",
            "speakingRate": 1.0,
            "pitch": 0,
        },
    }
    try:
        resp = requests.post(
            _TTS_URL,
            params={"key": key},
            json=payload,
            timeout=45,
        )
    except requests.RequestException as e:
        return None, f"tts_request_error:{e}"

    if not (200 <= resp.status_code < 300):
        return None, f"tts_http_{resp.status_code}:{(resp.text or '')[:500]}"

    data = resp.json() or {}
    b64 = data.get("audioContent")
    if not b64:
        return None, "tts_no_audio"
    try:
        return base64.b64decode(b64), None
    except Exception as e:
        return None, f"tts_decode_error:{e}"
