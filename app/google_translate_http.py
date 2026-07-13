"""Google Cloud Translation API v2 (REST) — shared by patient translation flow and /v1 ML HTTP routes."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import requests

from app.config import settings

_log = logging.getLogger(__name__)

_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
_DETECT_URL = "https://translation.googleapis.com/language/translate/v2/detect"

# Nigerian Pidgin (pcm) is not in Google Cloud Translation v2; use bundled Marian models instead.
_GOOGLE_UNSUPPORTED_LANGS = frozenset({"pcm"})


def google_translate_api_key() -> Optional[str]:
    return (settings.google_translate_api_key or "").strip() or None


def google_detect_language(text: str) -> Optional[Tuple[str, float]]:
    key = google_translate_api_key()
    if not key or not (text or "").strip():
        return None
    try:
        r = requests.post(
            f"{_DETECT_URL}?key={key}",
            json={"q": text[:5000]},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not (200 <= r.status_code < 300):
            _log.warning("Google detect HTTP %s", r.status_code)
            return None
        data = r.json()
        dets = (data.get("data") or {}).get("detections") or []
        if not dets or not dets[0]:
            return None
        first = dets[0][0]
        lang = str(first.get("language") or "").lower()
        conf = float(first.get("confidence") or 0.0)
        return lang, conf
    except Exception as exc:
        _log.warning("Google detect failed: %s", exc)
        return None


def google_translate_text(*, text: str, target: str, source: Optional[str]) -> Optional[str]:
    key = google_translate_api_key()
    if not key:
        return None
    tgt = (target or "en").lower().strip()
    src = source.lower().strip() if source else None
    if tgt in _GOOGLE_UNSUPPORTED_LANGS or (src and src in _GOOGLE_UNSUPPORTED_LANGS):
        _log.debug(
            "Google Translate skipped for %s (unsupported); use in-app Marian for pcm",
            tgt if tgt in _GOOGLE_UNSUPPORTED_LANGS else src,
        )
        return None
    body: dict = {"q": text[:15000], "target": tgt, "format": "text"}
    if src:
        body["source"] = src
    try:
        r = requests.post(
            f"{_TRANSLATE_URL}?key={key}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        if not (200 <= r.status_code < 300):
            _log.warning(
                "Google translate HTTP %s (%s -> %s): %s",
                r.status_code,
                src or "auto",
                tgt,
                (r.text or "")[:300],
            )
            return None
        data = r.json()
        trans = (data.get("data") or {}).get("translations") or []
        if not trans:
            return None
        out = trans[0].get("translatedText")
        if isinstance(out, str) and out.strip():
            return out.strip()
        return None
    except Exception as exc:
        _log.warning("Google translate failed: %s", exc)
        return None
