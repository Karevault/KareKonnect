"""
Neural language ID (FastText lid.176) + Marian MT for pcm ↔ English only.

Hausa, Igbo, Yoruba, and other LANG codes use Google Cloud Translation in app.translation (not Marian).

Also invoked in-process when HC_TRANSLATION_ML_BASE_URL matches the app’s own public URL.
"""

from __future__ import annotations

from typing import Literal, Optional, Tuple

from app.config import settings
from app.neural_lid import detect_language_neural, normalize_app_language_code
from app.neural_marian import translate_neural

TranslateCategory = Literal["from_en", "to_en"]


def proxy_model_version() -> Optional[str]:
    v = (settings.hc_translation_ml_model_version or "").strip()
    return v or None


def run_detect(text: str) -> Optional[Tuple[str, float, Optional[str]]]:
    """FastText language identification."""
    det = detect_language_neural(text)
    if not det:
        return None
    lang, conf, label = det
    mv = proxy_model_version() or label
    return lang, conf, mv


def run_translate(
    *,
    text: str,
    source_lang: str,
    target_lang: str,
    category: TranslateCategory,
) -> Optional[Tuple[str, float, Optional[str]]]:
    """
    Marian for pcm pairs only. Returns None → caller uses Google Translate (ha/ig/yo/en).
    """
    _ = category
    raw = (text or "").strip()
    if not raw:
        return None

    sl_in = source_lang.strip().lower()
    tl = normalize_app_language_code(target_lang)

    if sl_in in ("auto", ""):
        det = detect_language_neural(raw)
        sl = normalize_app_language_code(det[0]) if det else "en"
    else:
        sl = normalize_app_language_code(source_lang)

    out = translate_neural(text=raw, source_lang=sl, target_lang=tl)
    if out:
        translated, conf, model_id = out
        mv = proxy_model_version() or model_id
        return translated, conf, mv

    if sl == tl:
        return raw, 1.0, proxy_model_version() or "passthrough"

    return None
