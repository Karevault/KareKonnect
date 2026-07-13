"""Aggregates translation / ML metrics for the admin monitoring dashboard."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select

from app.config import settings
from app.database import session_scope
from app.models import TranslationCache, TranslationLearningSample, User
from app.neural_lid import is_fasttext_available, lid_model_path
from app.neural_marian import MARIAN_MODEL_IDS, marian_loaded_model_ids


def _hf_hub_dir() -> Path:
    hf = (os.getenv("HF_HOME") or "").strip()
    if hf:
        return Path(hf)
    return Path(__file__).resolve().parent.parent / "ml_cache" / "hf_hub"


def run_live_pcm_smoke() -> dict[str, Any]:
    """Quick en→pcm check (may load Marian weights on first run)."""
    from app.neural_marian import translate_neural
    from app.translation import translate_outbound_for_user

    sample = "Drink plenty of water and rest if you feel unwell."
    neural = translate_neural(text=sample, source_lang="en", target_lang="pcm")
    outbound = translate_outbound_for_user(sample, "pcm")
    pidgin = neural[0] if neural else None
    return {
        "english_sample": sample,
        "marian_ok": neural is not None,
        "marian_pidgin": pidgin,
        "marian_model": neural[2] if neural else None,
        "pipeline_ok": outbound.strip() != sample.strip(),
        "pipeline_text": outbound,
        "same_as_english": (pidgin or "").strip() == sample.strip(),
    }


def gather_translation_monitoring() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=24)).isoformat()
    since_7d = (now - timedelta(days=7)).isoformat()

    with session_scope() as s:
        cache_total = int(s.execute(select(func.count()).select_from(TranslationCache)).scalar_one() or 0)
        learning_total = int(
            s.execute(select(func.count()).select_from(TranslationLearningSample)).scalar_one() or 0
        )
        learning_24h = int(
            s.execute(
                select(func.count())
                .select_from(TranslationLearningSample)
                .where(TranslationLearningSample.created_at >= since_24h)
            ).scalar_one()
            or 0
        )
        learning_7d = int(
            s.execute(
                select(func.count())
                .select_from(TranslationLearningSample)
                .where(TranslationLearningSample.created_at >= since_7d)
            ).scalar_one()
            or 0
        )

        cache_by_provider = dict(
            s.execute(
                select(TranslationCache.provider, func.count())
                .group_by(TranslationCache.provider)
                .order_by(func.count().desc())
            ).all()
        )
        cache_by_category = dict(
            s.execute(
                select(TranslationCache.category, func.count())
                .group_by(TranslationCache.category)
                .order_by(func.count().desc())
            ).all()
        )
        learning_by_provider = dict(
            s.execute(
                select(TranslationLearningSample.provider, func.count())
                .group_by(TranslationLearningSample.provider)
                .order_by(func.count().desc())
            ).all()
        )
        pair_rows = s.execute(
            select(
                TranslationLearningSample.source_lang,
                TranslationLearningSample.target_lang,
                func.count(),
            )
            .group_by(
                TranslationLearningSample.source_lang,
                TranslationLearningSample.target_lang,
            )
            .order_by(func.count().desc())
            .limit(12)
        ).all()
        language_pairs = [
            {"source": src, "target": tgt, "count": cnt} for src, tgt, cnt in pair_rows
        ]

        users_by_lang = dict(
            s.execute(select(User.language, func.count()).group_by(User.language).order_by(func.count().desc())).all()
        )

        pcm_poisoned = int(
            s.execute(
                select(func.count())
                .select_from(TranslationCache)
                .where(
                    TranslationCache.target_lang == "pcm",
                    TranslationCache.result_text == TranslationCache.source_text,
                )
            ).scalar_one()
            or 0
        )

        stale_ml = 0
        expected_mv = (settings.hc_translation_ml_model_version or "").strip()
        if expected_mv:
            stale_ml = int(
                s.execute(
                    select(func.count())
                    .select_from(TranslationCache)
                    .where(
                        TranslationCache.provider == "ml",
                        (TranslationCache.ml_model_version.is_(None))
                        | (TranslationCache.ml_model_version != expected_mv),
                    )
                ).scalar_one()
                or 0
            )

        recent_learning = list(
            s.execute(
                select(TranslationLearningSample)
                .order_by(TranslationLearningSample.created_at.desc())
                .limit(25)
            )
            .scalars()
            .all()
        )
        for row in recent_learning:
            s.expunge(row)

        recent_cache = list(
            s.execute(
                select(TranslationCache)
                .order_by(TranslationCache.updated_at.desc())
                .limit(15)
            )
            .scalars()
            .all()
        )
        for row in recent_cache:
            s.expunge(row)

    hf_dir = _hf_hub_dir()
    hf_on_disk = hf_dir.is_dir()
    hf_size_mb: Optional[float] = None
    if hf_on_disk:
        try:
            total = sum(f.stat().st_size for f in hf_dir.rglob("*") if f.is_file())
            hf_size_mb = round(total / (1024 * 1024), 1)
        except OSError:
            hf_size_mb = None

    return {
        "generated_at": now.isoformat(),
        "config": {
            "record_learning": settings.hc_translation_record_learning,
            "ml_model_version": settings.hc_translation_ml_model_version or "—",
            "ml_min_confidence": settings.hc_translation_ml_min_confidence,
            "ml_base_url_set": bool((settings.hc_translation_ml_base_url or "").strip()),
            "google_translate_set": bool((settings.google_translate_api_key or "").strip()),
            "pcm_translate_timeout_sec": os.getenv("HC_PCM_TRANSLATE_TIMEOUT_SEC", "18"),
        },
        "assets": {
            "fasttext_lid_path": str(lid_model_path()),
            "fasttext_available": is_fasttext_available(),
            "hf_home": str(hf_dir),
            "hf_on_disk": hf_on_disk,
            "hf_size_mb": hf_size_mb,
            "marian_models": list(dict.fromkeys(MARIAN_MODEL_IDS.values())),
            "marian_loaded": marian_loaded_model_ids(),
        },
        "counts": {
            "cache_total": cache_total,
            "learning_total": learning_total,
            "learning_24h": learning_24h,
            "learning_7d": learning_7d,
            "pcm_poisoned_cache": pcm_poisoned,
            "stale_ml_cache": stale_ml,
        },
        "cache_by_provider": cache_by_provider,
        "cache_by_category": cache_by_category,
        "learning_by_provider": learning_by_provider,
        "language_pairs": language_pairs,
        "users_by_lang": users_by_lang,
        "recent_learning": recent_learning,
        "recent_cache": recent_cache,
    }
