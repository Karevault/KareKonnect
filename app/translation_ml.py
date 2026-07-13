"""
HTTP client for your first-party translation / language-ID service.

When HC_TRANSLATION_ML_BASE_URL matches this deployment’s HC_BASE_URL (integration DB or HC_BASE_URL env),
requests are handled in-process via app.ml_translation_impl: **FastText lid.176** (detect) and
**Marian** (translate) for en↔pcm only. **Google Cloud Translation** handles ha, ig, yo, and en when Marian returns None.
Set `GOOGLE_TRANSLATE_API_KEY` for non-Pidgin languages (Google does not support pcm).

External hosts may implement the same JSON contract at POST …/v1/detect and POST …/v1/translate.

**POST {HC_TRANSLATION_ML_BASE_URL}/v1/detect**
Request JSON: `{"text": "<up to 5000 chars>"}`
Response 200 JSON: `{"language": "ha", "confidence": 0.92, "model_version": "ld-2"}`
`model_version` is optional; returned value is stored on cache rows for invalidation.

**POST {HC_TRANSLATION_ML_BASE_URL}/v1/translate**
Request JSON:
`{"text": "<...>", "source_lang": "en"|"auto", "target_lang": "ha", "category": "from_en"|"to_en"}`
Response 200 JSON:
`{"translated_text": "...", "confidence": 0.88, "model_version": "nmt-3"}`
If confidence is below HC_TRANSLATION_ML_MIN_CONFIDENCE, Kareconnect falls back to Google (when configured).

Optional header on both routes: `Authorization: Bearer <HC_TRANSLATION_ML_API_KEY>` when the env var is set.

Non-2xx or malformed responses are treated as “ML unavailable” and the app continues with Google / cache only.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional, Tuple

import requests

from app.config import settings

_log = logging.getLogger(__name__)

TranslateCategory = Literal["from_en", "to_en"]


def _ml_base_url() -> Optional[str]:
    u = (settings.hc_translation_ml_base_url or "").strip().rstrip("/")
    return u or None


def _internal_ml_same_origin() -> bool:
    """Use in-process proxy when ML URL is this app’s own public URL (same Railway service)."""
    ml = (_ml_base_url() or "").strip().rstrip("/").lower()
    if not ml:
        return False
    candidates: list[str] = []
    b = (settings.base_url or "").strip().rstrip("/").lower()
    if b:
        candidates.append(b)
    try:
        from app.integration_config import get_effective_integration

        ib = (get_effective_integration().hc_base_url or "").strip().rstrip("/").lower()
        if ib:
            candidates.append(ib)
    except Exception:
        pass
    return ml in [c for c in candidates if c]


def _ml_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = (settings.hc_translation_ml_api_key or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _apply_min_detect(res: Tuple[str, float, Optional[str]]) -> Optional[Tuple[str, float, Optional[str]]]:
    lang, conf, mv = res
    if conf < settings.hc_translation_ml_min_confidence:
        return None
    return lang, conf, mv


def _apply_min_translate(res: Tuple[str, float, Optional[str]]) -> Optional[Tuple[str, float, Optional[str]]]:
    text, conf, mv = res
    if conf < settings.hc_translation_ml_min_confidence:
        return None
    return text, conf, mv


def _use_inprocess_ml() -> bool:
    """Bundled FastText + Marian run in this process (no separate ML service required)."""
    return not _ml_base_url() or _internal_ml_same_origin()


def ml_detect_language(text: str) -> Optional[Tuple[str, float, Optional[str]]]:
    """
    Returns (language, confidence, model_version) or None if ML is not configured or call fails.
    """
    if not (text or "").strip():
        return None

    if _use_inprocess_ml():
        from app.ml_translation_impl import run_detect

        raw = run_detect(text)
        if raw:
            applied = _apply_min_detect(raw)
            if applied:
                return applied

    base = _ml_base_url()
    if not base or _internal_ml_same_origin():
        return None

    try:
        r = requests.post(
            f"{base}/v1/detect",
            json={"text": text[:5000]},
            headers=_ml_headers(),
            timeout=12,
        )
        if not (200 <= r.status_code < 300):
            _log.debug("ML detect HTTP %s", r.status_code)
            return None
        data = r.json()
        lang = str(data.get("language") or "").lower().strip()
        conf = float(data.get("confidence") or 0.0)
        mv = data.get("model_version")
        model_version = str(mv).strip() if mv is not None else None
        if not lang:
            return None
        if conf < settings.hc_translation_ml_min_confidence:
            return None
        return lang, conf, model_version or None
    except Exception as exc:
        _log.debug("ML detect failed: %s", exc)
        return None


def ml_translate(
    *,
    text: str,
    source_lang: str,
    target_lang: str,
    category: TranslateCategory,
) -> Optional[Tuple[str, float, Optional[str]]]:
    """
    Returns (translated_text, confidence, model_version) or None if ML is unavailable / low confidence.
    """
    if not (text or "").strip():
        return None

    if _use_inprocess_ml():
        from app.ml_translation_impl import run_translate

        raw = run_translate(
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            category=category,
        )
        if raw:
            applied = _apply_min_translate(raw)
            if applied:
                return applied

    base = _ml_base_url()
    if not base or _internal_ml_same_origin():
        return None

    try:
        r = requests.post(
            f"{base}/v1/translate",
            json={
                "text": text[:15000],
                "source_lang": source_lang,
                "target_lang": target_lang,
                "category": category,
            },
            headers=_ml_headers(),
            timeout=18,
        )
        if not (200 <= r.status_code < 300):
            _log.debug("ML translate HTTP %s", r.status_code)
            return None
        data = r.json()
        out = data.get("translated_text")
        if not isinstance(out, str) or not out.strip():
            return None
        conf = float(data.get("confidence") or 0.0)
        if conf < settings.hc_translation_ml_min_confidence:
            return None
        mv = data.get("model_version")
        model_version = str(mv).strip() if mv is not None else None
        return out.strip(), conf, model_version or None
    except Exception as exc:
        _log.debug("ML translate failed: %s", exc)
        return None
