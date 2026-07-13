"""
Patient-facing translation: DB cache → Marian (pcm only) → Google Cloud (ha/ig/yo).

Learning: append-only rows in translation_learning_samples when cache rows change.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Literal, Optional, Tuple

from app.config import settings
from app.google_translate_http import google_detect_language, google_translate_text
from app.neural_marian import translate_neural
from app.translation_cache import (
    get_cached_detection,
    get_cached_translation,
    put_cached_detection,
    put_cached_translation,
)
from app.translation_ml import ml_detect_language, ml_translate

_log = logging.getLogger(__name__)

CacheCategory = Literal["to_en", "from_en"]

_PCM_TRANSLATE_TIMEOUT_SEC = float(os.getenv("HC_PCM_TRANSLATE_TIMEOUT_SEC", "18"))
_pcm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcm-translate")


def _google_target_language_code(user_lang: str) -> str:
    u = (user_lang or "en").lower().strip()
    if u == "pcm":
        return "pcm"
    if u in ("ha", "ig", "yo"):
        return u
    return "en"


def detect_primary_language(text: str) -> Optional[tuple[str, float]]:
    if not (text or "").strip():
        return None
    cached = get_cached_detection(text)
    if cached:
        return cached
    ml = ml_detect_language(text)
    if ml:
        lang, conf, mv = ml
        put_cached_detection(text, lang, conf, provider="ml", ml_model_version=mv)
        return lang, conf
    g = google_detect_language(text)
    if not g:
        return None
    lang, conf = g
    put_cached_detection(text, lang, conf, provider="google", ml_model_version=None)
    return lang, conf


def translate_text(
    *,
    text: str,
    target: str,
    source: Optional[str] = None,
    cache_category: CacheCategory = "from_en",
) -> str:
    if not (text or "").strip():
        return text
    tgt = (target or "en").lower().strip()
    src_key = (source or "auto").lower().strip()
    cached = get_cached_translation(
        category=cache_category, source_lang=src_key, target_lang=tgt, text=text
    )
    if cached is not None:
        if not (tgt == "pcm" and cached.strip() == text.strip()):
            return cached

    ml_raw: Optional[Tuple[str, float, Optional[str]]] = None
    if tgt == "pcm" or src_key == "pcm":
        sl = "en" if src_key in ("auto", "") else src_key
        neural = translate_neural(text=text, source_lang=sl, target_lang=tgt)
        if neural:
            ml_raw = (neural[0], neural[1], neural[2])
    if ml_raw is None:
        ml_raw = ml_translate(
            text=text,
            source_lang=src_key,
            target_lang=tgt,
            category=cache_category,
        )
    if ml_raw:
        out, _conf, mv = ml_raw
        put_cached_translation(
            category=cache_category,
            source_lang=src_key,
            target_lang=tgt,
            text=text,
            result_text=out,
            provider="ml",
            ml_model_version=mv,
        )
        return out
    result = google_translate_text(text=text, target=tgt, source=source)
    if result:
        put_cached_translation(
            category=cache_category,
            source_lang=src_key,
            target_lang=tgt,
            text=text,
            result_text=result,
            provider="google",
            ml_model_version=None,
        )
        return result
    if tgt != "en" or src_key not in ("en", "auto"):
        _log.warning(
            "Translation failed (%s -> %s); returning English. google_api_key=%s",
            src_key,
            tgt,
            bool(settings.google_translate_api_key),
        )
    return text


def text_for_english_pipeline(body: str, user_lang: str) -> str:
    ul = (user_lang or "en").lower().strip()
    if ul == "en":
        return body
    det = detect_primary_language(body)
    if det:
        lang, _conf = det
        if lang == "en":
            return body
    return translate_text(text=body, target="en", source=ul, cache_category="to_en")


def translate_outbound_for_user(text: str, user_lang: str) -> str:
    """English compose → patient LANG. Pidgin uses Marian with timeout; never returns empty."""
    if not (text or "").strip():
        return text
    ul = (user_lang or "en").lower().strip()
    if ul == "en":
        return text
    tgt = _google_target_language_code(ul)
    if tgt == "en":
        return text

    if ul == "pcm":
        future = _pcm_executor.submit(
            translate_text,
            text=text,
            target=tgt,
            source="en",
            cache_category="from_en",
        )
        try:
            out = future.result(timeout=_PCM_TRANSLATE_TIMEOUT_SEC)
            if (out or "").strip():
                return out
        except FuturesTimeoutError:
            _log.warning(
                "Pidgin outbound translation timed out after %ss; using English",
                _PCM_TRANSLATE_TIMEOUT_SEC,
            )
        except Exception as exc:
            _log.warning("Pidgin outbound translation failed: %s", exc)
        return text

    return translate_text(text=text, target=tgt, source="en", cache_category="from_en")


def translate_staff_reply_to_patient(reply_text: str, user_lang: str) -> str:
    return translate_outbound_for_user(reply_text, user_lang)
