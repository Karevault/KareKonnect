"""DB-backed translation cache (category + language pair); ML-managed upserts + learning samples."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal, Optional

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import session_scope
from app.models import TranslationCache
from app.translation_learning import record_translation_learning_sample
from app.util import now_iso

_log = logging.getLogger(__name__)

TranslationCategory = Literal["language_detect", "to_en", "from_en"]

_MAX_SOURCE_LEN = 15000


def _normalize_source(text: str) -> str:
    return (text or "")[:_MAX_SOURCE_LEN].strip()


def make_cache_id(*, category: str, source_lang: str, target_lang: str, text: str) -> str:
    norm = _normalize_source(text)
    raw = f"{category}\x1f{source_lang.lower()}\x1f{target_lang.lower()}\x1f{norm}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row_ml_stale(row: TranslationCache) -> bool:
    """When you bump HC_TRANSLATION_ML_MODEL_VERSION, cached ML rows are refreshed."""
    expected = (settings.hc_translation_ml_model_version or "").strip()
    if not expected:
        return False
    if (row.provider or "") != "ml":
        return False
    return (row.ml_model_version or "") != expected


def get_cached_translation(
    *, category: TranslationCategory, source_lang: str, target_lang: str, text: str
) -> Optional[str]:
    """Return cached translated string, or None (including when ML cache is stale for a new model)."""
    cid = make_cache_id(category=category, source_lang=source_lang, target_lang=target_lang, text=text)
    with session_scope() as s:
        row = s.get(TranslationCache, cid)
        if not row:
            return None
        if _row_ml_stale(row):
            return None
        out = (row.result_text or "").strip()
        return out if out else None


def put_cached_translation(
    *,
    category: TranslationCategory,
    source_lang: str,
    target_lang: str,
    text: str,
    result_text: str,
    provider: str,
    ml_model_version: Optional[str] = None,
) -> None:
    if not (result_text or "").strip():
        return
    norm = _normalize_source(text)
    if not norm:
        return
    cid = make_cache_id(category=category, source_lang=source_lang, target_lang=target_lang, text=text)
    now = now_iso()
    prov = (provider or "google").lower()[:16]
    if prov not in ("ml", "google"):
        prov = "google"
    mv = (ml_model_version or "").strip()[:64] or None

    def _apply(row: TranslationCache) -> None:
        row.result_text = result_text
        row.provider = prov
        row.ml_model_version = mv
        row.updated_at = now

    should_record = False
    try:
        with session_scope() as s:
            row = s.get(TranslationCache, cid)
            if row:
                if (
                    row.result_text != result_text
                    or row.provider != prov
                    or (row.ml_model_version or "") != (mv or "")
                ):
                    _apply(row)
                    should_record = True
            else:
                s.add(
                    TranslationCache(
                        id=cid,
                        category=category,
                        source_lang=source_lang.lower()[:16],
                        target_lang=target_lang.lower()[:16],
                        source_text=norm,
                        result_text=result_text,
                        provider=prov,
                        ml_model_version=mv,
                        created_at=now,
                        updated_at=now,
                    )
                )
                should_record = True
    except IntegrityError:
        with session_scope() as s:
            row = s.get(TranslationCache, cid)
            if row:
                if (
                    row.result_text != result_text
                    or row.provider != prov
                    or (row.ml_model_version or "") != (mv or "")
                ):
                    _apply(row)
                    should_record = True

    if should_record:
        record_translation_learning_sample(
            cache_id=cid,
            category=category,
            source_lang=source_lang.lower()[:16],
            target_lang=target_lang.lower()[:16],
            source_text=norm,
            output_text=result_text[:15000],
            provider=prov,
            ml_model_version=mv,
        )


def get_cached_detection(text: str) -> Optional[tuple[str, float]]:
    """Return (language, confidence) from cache, or None."""
    cid = make_cache_id(category="language_detect", source_lang="_", target_lang="_", text=text)
    with session_scope() as s:
        row = s.get(TranslationCache, cid)
        if not row or row.category != "language_detect":
            return None
        if _row_ml_stale(row):
            return None
        try:
            data = json.loads(row.result_text)
            lang = str(data.get("language") or "").lower()
            conf = float(data.get("confidence") or 0.0)
            if not lang:
                return None
            return lang, conf
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


def put_cached_detection(
    text: str,
    language: str,
    confidence: float,
    *,
    provider: str = "google",
    ml_model_version: Optional[str] = None,
) -> None:
    norm = _normalize_source(text)
    if not norm:
        return
    payload = json.dumps({"language": language.lower(), "confidence": confidence})
    cid = make_cache_id(category="language_detect", source_lang="_", target_lang="_", text=text)
    now = now_iso()
    prov = (provider or "google").lower()[:16]
    if prov not in ("ml", "google"):
        prov = "google"
    mv = (ml_model_version or "").strip()[:64] or None

    def _apply(row: TranslationCache) -> None:
        row.result_text = payload
        row.provider = prov
        row.ml_model_version = mv
        row.updated_at = now

    should_record = False
    try:
        with session_scope() as s:
            row = s.get(TranslationCache, cid)
            if row:
                if (
                    row.result_text != payload
                    or row.provider != prov
                    or (row.ml_model_version or "") != (mv or "")
                ):
                    _apply(row)
                    should_record = True
            else:
                s.add(
                    TranslationCache(
                        id=cid,
                        category="language_detect",
                        source_lang="_",
                        target_lang="_",
                        source_text=norm,
                        result_text=payload,
                        provider=prov,
                        ml_model_version=mv,
                        created_at=now,
                        updated_at=now,
                    )
                )
                should_record = True
    except IntegrityError:
        with session_scope() as s:
            row = s.get(TranslationCache, cid)
            if row:
                if (
                    row.result_text != payload
                    or row.provider != prov
                    or (row.ml_model_version or "") != (mv or "")
                ):
                    _apply(row)
                    should_record = True

    if should_record:
        record_translation_learning_sample(
            cache_id=cid,
            category="language_detect",
            source_lang="_",
            target_lang="_",
            source_text=norm,
            output_text=payload,
            provider=prov,
            ml_model_version=mv,
        )
