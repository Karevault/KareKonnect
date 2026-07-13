"""
Internal translation ML HTTP API (mounted on the same FastAPI app).

Uses FastText lid.176 + Marian for pcm only (see app/neural_lid.py, app/neural_marian.py).
Hausa/Igbo/Yoruba translation is via Google Cloud Translation in app.translation, not this API.

Set HC_TRANSLATION_ML_BASE_URL to your public site URL (same as HC_BASE_URL) for in-process routing.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.ml_translation_impl import run_detect, run_translate
from app.neural_lid import is_fasttext_available

router = APIRouter(prefix="/v1", tags=["translation-ml"])


def _require_ml_bearer(authorization: Optional[str] = Header(None)) -> None:
    expected = (settings.hc_translation_ml_api_key or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="HC_TRANSLATION_ML_API_KEY is not set; configure it to use /v1/detect and /v1/translate",
        )
    parts = (authorization or "").split(None, 1)
    if len(parts) < 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization: Bearer <HC_TRANSLATION_ML_API_KEY> required")
    token = parts[1].strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


class DetectRequest(BaseModel):
    text: str = Field(..., max_length=5000)


class DetectResponse(BaseModel):
    language: str
    confidence: float
    model_version: Optional[str] = None


class TranslateRequest(BaseModel):
    text: str = Field(..., max_length=15000)
    source_lang: str = Field(..., description='e.g. "en", "auto", "ha"')
    target_lang: str
    category: Literal["from_en", "to_en"]


class TranslateResponse(BaseModel):
    translated_text: str
    confidence: float
    model_version: Optional[str] = None


@router.post("/detect", response_model=DetectResponse)
def post_detect(body: DetectRequest, _: None = Depends(_require_ml_bearer)) -> DetectResponse:
    if not is_fasttext_available():
        raise HTTPException(
            status_code=503,
            detail=f"FastText lid model missing (expected at deploy via scripts/download_ml_assets.py). "
            f"See ml_cache/lid.176.ftz",
        )
    raw = run_detect(body.text)
    if not raw:
        raise HTTPException(status_code=502, detail="Language detection failed")
    lang, conf, mv = raw
    return DetectResponse(language=lang, confidence=conf, model_version=mv)


@router.post("/translate", response_model=TranslateResponse)
def post_translate(body: TranslateRequest, _: None = Depends(_require_ml_bearer)) -> TranslateResponse:
    raw = run_translate(
        text=body.text,
        source_lang=body.source_lang,
        target_lang=body.target_lang,
        category=body.category,
    )
    if not raw:
        raise HTTPException(
            status_code=502,
            detail="Neural translation unavailable for this pair (only en↔pcm supported here). "
            "Use GOOGLE_TRANSLATE_API_KEY on the main app for ha/ig/yo.",
        )
    out, conf, mv = raw
    return TranslateResponse(translated_text=out, confidence=conf, model_version=mv)
