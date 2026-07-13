"""Google Cloud Vision API — OCR and image analysis (REST, API key)."""

from __future__ import annotations

import base64
from io import BytesIO
import logging
from typing import Any, Optional

import requests

from app.config import settings

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - dependency presence is environment-specific.
    PdfReader = None

_log = logging.getLogger(__name__)
_VISION_URL = "https://vision.googleapis.com/v1/images:annotate"


def vision_api_key() -> Optional[str]:
    key = (settings.google_cloud_api_key or settings.google_translate_api_key or "").strip()
    return key or None


def is_vision_configured() -> bool:
    return bool(vision_api_key())


def _annotate(image_bytes: bytes, features: list[dict[str, str]]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    key = vision_api_key()
    if not key:
        return None, "google_vision_not_configured"
    if not image_bytes:
        return None, "empty_image"
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": features,
            }
        ]
    }
    try:
        resp = requests.post(_VISION_URL, params={"key": key}, json=payload, timeout=45)
    except requests.RequestException as exc:
        return None, str(exc)
    if not (200 <= resp.status_code < 300):
        return None, f"vision_http_{resp.status_code}:{(resp.text or '')[:400]}"
    data = resp.json() or {}
    responses = data.get("responses") or []
    if not responses:
        return None, "vision_empty_response"
    return responses[0], None


def extract_text_ocr(image_bytes: bytes) -> tuple[Optional[str], Optional[str]]:
    """Document / image OCR via TEXT_DETECTION."""
    result, err = _annotate(image_bytes, [{"type": "TEXT_DETECTION", "maxResults": 1}])
    if err or not result:
        return None, err
    if "error" in result:
        return None, str(result["error"])
    full = result.get("fullTextAnnotation") or {}
    text = (full.get("text") or "").strip()
    if text:
        return text, None
    annotations = result.get("textAnnotations") or []
    if annotations and isinstance(annotations[0], dict):
        return (annotations[0].get("description") or "").strip() or None, None
    return None, None


def extract_document_text(document_bytes: bytes, *, mime_type: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Extract text from a PDF when possible; otherwise fall back to image OCR."""
    mime = (mime_type or "").lower()
    if "pdf" in mime or document_bytes.startswith(b"%PDF"):
        return _extract_pdf_text(document_bytes)
    return extract_text_ocr(document_bytes)


def _extract_pdf_text(document_bytes: bytes) -> tuple[Optional[str], Optional[str]]:
    if not document_bytes:
        return None, "empty_document"
    if PdfReader is None:
        return None, "pdf_text_extractor_not_installed"
    try:
        reader = PdfReader(BytesIO(document_bytes))
    except Exception as exc:
        _log.warning("pdf parse failed: %s", exc)
        return None, f"pdf_parse_failed:{str(exc)[:160]}"

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:
            _log.warning("pdf page text extraction failed: %s", exc)
            return None, f"pdf_text_extract_failed:{str(exc)[:160]}"
        if text:
            pages.append(text)
    if not pages:
        return None, "pdf_no_extractable_text"
    return "\n\n".join(pages), None


def analyze_image(image_bytes: bytes, *, hint: Optional[str] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """
    Label detection + safe search. Returns dict with labels, safe_search, preliminary_summary.
    """
    result, err = _annotate(
        image_bytes,
        [
            {"type": "LABEL_DETECTION", "maxResults": 12},
            {"type": "SAFE_SEARCH_DETECTION", "maxResults": 1},
        ],
    )
    if err or not result:
        return None, err
    if "error" in result:
        return None, str(result["error"])

    labels = [
        (item.get("description") or "").strip()
        for item in (result.get("labelAnnotations") or [])
        if isinstance(item, dict) and item.get("description")
    ]
    safe = result.get("safeSearchAnnotation") or {}
    category = _infer_media_category(labels, hint=hint)
    summary = _build_preliminary_summary(labels=labels, category=category, hint=hint)
    return {
        "labels": labels,
        "safe_search": safe,
        "media_category": category,
        "preliminary_summary": summary,
    }, None


def _infer_media_category(labels: list[str], *, hint: Optional[str] = None) -> str:
    blob = " ".join(labels).lower()
    hint_l = (hint or "").lower()
    combined = f"{blob} {hint_l}"
    if any(w in combined for w in ("eye", "iris", "pupil", "conjunctiv", "sclera", "eyelid")):
        return "eye"
    if any(w in combined for w in ("wound", "injury", "laceration", "ulcer", "sore", "bruise", "burn")):
        return "wound"
    if any(w in combined for w in ("rash", "dermatitis", "eczema", "skin", "acne", "lesion", "blister")):
        return "rash"
    if any(w in combined for w in ("document", "prescription", "receipt", "paper", "text")):
        return "document"
    return "other"


def _build_preliminary_summary(*, labels: list[str], category: str, hint: Optional[str]) -> str:
    top = ", ".join(labels[:5]) if labels else "no visual labels"
    hint_s = (hint or "").strip()
    prefix = {
        "eye": "Eye photo (preliminary):",
        "wound": "Wound/injury photo (preliminary):",
        "rash": "Skin/rash photo (preliminary):",
        "document": "Document image (preliminary):",
        "other": "Clinical image (preliminary):",
    }.get(category, "Image (preliminary):")
    parts = [f"{prefix} detected features include {top}."]
    if hint_s:
        parts.append(f"Patient note: {hint_s[:300]}")
    parts.append(
        "This is not a diagnosis — a clinician must review the image. "
        "Seek emergency care for severe pain, spreading redness, vision loss, or heavy bleeding."
    )
    return " ".join(parts)
