"""Append-only training samples when translations are resolved (ML or Google)."""

from __future__ import annotations

from app.config import settings
from app.database import session_scope
from app.models import TranslationLearningSample
from app.util import new_id, now_iso


def record_translation_learning_sample(
    *,
    cache_id: str,
    category: str,
    source_lang: str,
    target_lang: str,
    source_text: str,
    output_text: str,
    provider: str,
    ml_model_version: str | None,
) -> None:
    if not settings.hc_translation_record_learning:
        return
    if not (output_text or "").strip():
        return
    row_id = new_id("tls")
    now = now_iso()
    with session_scope() as s:
        s.add(
            TranslationLearningSample(
                id=row_id,
                cache_id=cache_id[:64],
                category=category[:32],
                source_lang=source_lang[:16],
                target_lang=target_lang[:16],
                source_text=source_text[:15000],
                output_text=output_text[:15000],
                provider=provider[:16],
                ml_model_version=(ml_model_version or "")[:64] or None,
                created_at=now,
            )
        )
