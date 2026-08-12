"""Inbound patient images/documents — storage, OCR, vision, provider routing."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from sqlalchemy import select

from app import meta_whatsapp as meta_wa
from app.clinical_intelligence import (
    clinical_intelligence_enabled,
    format_assessment_for_patient,
    run_clinical_assessment,
)
from app.database import session_scope
from app.google_vision import analyze_image, extract_document_text, extract_text_ocr, is_vision_configured
from app.integration_config import get_effective_integration
from app.messaging import send_message, send_whatsapp_broadcast
from app.models import PatientMediaAttachment, TelehealthProvider, TelehealthSession, User
from app.services import active_program_codes_for_phone_e164
from app.telehealth import mask_phone_for_display
from app.triage import (
    assessment_program_context,
    current_intake_prompt,
    get_active_telehealth_session_id,
    get_active_triage_session,
    intake_briefing_due,
    intake_complete,
    start_triage_session,
)
from app.util import new_id, now_iso, normalize_phone_e164, safe_json_dumps

_log = logging.getLogger(__name__)
Channel = Literal["sms", "whatsapp"]


def attachments_dir() -> Path:
    raw = (os.getenv("HC_ATTACHMENTS_DIR") or "data/attachments").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _extension_for_mime(mime: Optional[str], media_type: str) -> str:
    m = (mime or "").lower()
    if "pdf" in m:
        return ".pdf"
    if "png" in m:
        return ".png"
    if "webp" in m:
        return ".webp"
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if media_type == "document":
        return ".pdf"
    return ".bin"


_LAB_VALUE_RE = re.compile(
    r"(?im)\b(ha1c|hba1c|blood\s*(?:glucose|sugar)|glucose|cholesterol|ldl|hdl|"
    r"triglyceride|creatinine|urea|potassium|sodium|haemoglobin|hemoglobin|"
    r"white\s*blood\s*cell|wbc|platelet|blood\s*pressure|bp|pulse|heart\s*rate)\b"
    r"[\s:]*([0-9]+(?:\.[0-9]+)?)\s*(mg/dl|mmol/l|g/dl|%|mmhg|bpm|x\s*10\^?[0-9]/l)?"
)


def _extract_lab_summary(ocr_text: Optional[str]) -> str:
    """Extract key lab values from OCR text for a concise patient-facing summary."""
    if not ocr_text:
        return ""
    hits: list[str] = []
    seen: set[str] = set()
    for m in _LAB_VALUE_RE.finditer(ocr_text):
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        unit = (m.group(3) or "").strip()
        key = f"{label}:{value}"
        if key not in seen:
            seen.add(key)
            unit_str = f" {unit}" if unit else ""
            hits.append(f"{label.title()}: {value}{unit_str}")
    if not hits:
        return ""
    return "Key values: " + " | ".join(hits[:6])


def _join_processing_errors(*errors: Optional[str]) -> Optional[str]:
    parts: list[str] = []
    seen: set[str] = set()
    for err in errors:
        cleaned = (err or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            parts.append(cleaned)
    return "; ".join(parts) or None


def _extract_media_insights(
    *,
    data: bytes,
    media_type: str,
    mime_type: Optional[str],
    caption: Optional[str],
) -> dict[str, Any]:
    ocr_text: Optional[str] = None
    vision_summary: Optional[str] = None
    vision_labels: Optional[list[str]] = None
    media_category = "document" if media_type == "document" else "other"

    mime = (mime_type or "").lower()
    is_pdf_document = media_type == "document" or "pdf" in mime
    vision_enabled = is_vision_configured()

    if is_pdf_document:
        ocr_text, doc_err = extract_document_text(data, mime_type=mime_type)
        if not vision_enabled and caption and not ocr_text:
            vision_summary = f"Document received. Patient caption: {caption[:400]}"
        return {
            "ocr_text": ocr_text,
            "vision_summary": vision_summary,
            "vision_labels": vision_labels,
            "media_category": "document",
            "processing_error": doc_err,
        }

    if not vision_enabled:
        if caption:
            vision_summary = f"Image received. Patient caption: {caption[:400]}"
        return {
            "ocr_text": None,
            "vision_summary": vision_summary,
            "vision_labels": None,
            "media_category": media_category,
            "processing_error": "vision_not_configured",
        }

    ocr_text, ocr_err = extract_text_ocr(data)
    analysis, vis_err = analyze_image(data, hint=caption)
    if analysis:
        vision_summary = analysis.get("preliminary_summary")
        vision_labels = analysis.get("labels") or []
        media_category = analysis.get("media_category") or media_category

    return {
        "ocr_text": ocr_text,
        "vision_summary": vision_summary,
        "vision_labels": vision_labels,
        "media_category": media_category,
        "processing_error": _join_processing_errors(ocr_err, vis_err),
    }


def process_inbound_whatsapp_media(
    *,
    phone_e164: str,
    media_type: str,
    media_id: str,
    mime_type: Optional[str],
    caption: Optional[str],
    filename: Optional[str],
    meta_message_id: Optional[str],
) -> str:
    """
    Download, store, OCR/vision, clinical assessment, notify provider if applicable.
    Returns patient-facing reply text.
    """
    phone = normalize_phone_e164(phone_e164)
    mtype = (media_type or "image").strip().lower()
    if mtype not in ("image", "document", "video"):
        return "We received your file. Please send a photo or document (PDF/image) with a short description."

    data, mime, dl_err = meta_wa.download_media(media_id)
    if dl_err or not data:
        _log.warning("media download failed: %s", dl_err)
        return "We could not download your file. Please try again or send a smaller photo."

    sha = hashlib.sha256(data).hexdigest()
    aid = new_id("att")
    ext = _extension_for_mime(mime, mtype)
    path = attachments_dir() / f"{aid}{ext}"
    path.write_bytes(data)

    triage = get_active_triage_session(phone)
    triage_id = triage.id if triage else None
    ths_id = get_active_telehealth_session_id(phone)
    if not triage_id and not ths_id:
        triage_id = start_triage_session(phone_e164=phone, channel="whatsapp")

    user_id: Optional[str] = None
    with session_scope() as s:
        user_id = s.execute(select(User.id).where(User.phone_e164 == phone)).scalar_one_or_none()
        s.add(
            PatientMediaAttachment(
                id=aid,
                user_id=user_id,
                phone_e164=phone,
                channel="whatsapp",
                media_type=mtype,
                mime_type=mime,
                filename=filename,
                caption=(caption or "").strip() or None,
                storage_path=str(path),
                sha256=sha,
                meta_media_id=media_id,
                triage_session_id=triage_id,
                telehealth_session_id=ths_id,
                processing_status="pending",
                created_at=now_iso(),
            )
        )

    insights = _extract_media_insights(data=data, media_type=mtype, mime_type=mime, caption=caption)
    ocr_text = insights["ocr_text"]
    vision_summary = insights["vision_summary"]
    vision_labels = insights["vision_labels"]
    media_category = insights["media_category"]
    proc_err = insights["processing_error"]

    with session_scope() as s:
        row = s.get(PatientMediaAttachment, aid)
        if row:
            row.ocr_text = (ocr_text or "")[:8000] or None
            row.vision_summary = (vision_summary or "")[:4000] or None
            row.vision_labels_json = safe_json_dumps(vision_labels) if vision_labels else None
            row.media_category = media_category
            row.processing_status = "failed" if proc_err and not vision_summary and not ocr_text else "processed"
            row.processing_error = (proc_err or "")[:500] or None

    assessment_text = ""
    glucose_block = ""
    combined_text = f"{caption or ''}\n{ocr_text or ''}"
    show_intake_briefing = intake_briefing_due(phone_e164=phone, triage_session_id=triage_id)
    if clinical_intelligence_enabled() and intake_complete(triage_session_id=triage_id):
        from app.clinical_actions import execute_glucose_monitoring_path, format_glucose_patient_reply
        from app.glucose_triage import extract_glucose_from_text, is_glucose_related

        programs = assessment_program_context(
            programs=active_program_codes_for_phone_e164(phone),
            triage_session_id=triage_id,
            chief_complaint=combined_text,
        )
        result = run_clinical_assessment(
            phone_e164=phone,
            patient_text=combined_text,
            ocr_text=ocr_text,
            vision_summaries=[vision_summary] if vision_summary else None,
            media_categories=[media_category],
            programs=programs,
            triage_session_id=triage_id,
            attachment_ids=[aid],
            source="media",
            user_id=user_id,
        )
        if is_glucose_related(combined_text) or extract_glucose_from_text(combined_text):
            extracted = extract_glucose_from_text(combined_text)
            if extracted:
                with session_scope() as s:
                    row = s.get(PatientMediaAttachment, aid)
                    if row:
                        row.media_category = "glucose"
                plan = execute_glucose_monitoring_path(
                    phone_e164=phone,
                    user_id=user_id,
                    ocr_text=ocr_text,
                    caption=caption,
                    attachment_id=aid,
                    assessment_id=result.assessment_id,
                    triage_session_id=triage_id,
                )
                if plan:
                    glucose_block = format_glucose_patient_reply(
                        plan,
                        extracted_value=extracted.value_mg_dl,
                        reading_type=extracted.reading_type,
                    )
        if not glucose_block and show_intake_briefing:
            if result.urgency_tier == "emergency":
                assessment_text = ""
            else:
                assessment_text = format_assessment_for_patient(result)

    if ths_id:
        notify_provider_media(attachment_id=aid, telehealth_session_id=ths_id)

    _notify_operator_media(attachment_id=aid, phone_e164=phone, triage_session_id=triage_id, ths_id=ths_id)

    lines = [f"Received your {'document' if mtype == 'document' else 'photo'} (ref {aid.replace('att_', 'M-')[:12].upper()})."]
    if vision_summary:
        lines.append(vision_summary[:650])
    elif ocr_text:
        preview = re.sub(r"\s+", " ", ocr_text).strip()[:400]
        lines.append(f"Document text (extract): {preview}")
    lab_summary = _extract_lab_summary(ocr_text)
    if lab_summary:
        lines.append(lab_summary)
    if glucose_block:
        lines.append(glucose_block)
    elif assessment_text:
        lines.append(assessment_text)
    elif triage_id and not intake_complete(triage_session_id=triage_id):
        lines.append("Before clinical review, answer these quick intake questions.")
        lines.append(current_intake_prompt(triage_session_id=triage_id))
    if ths_id:
        lines.append("Shared with your telehealth clinician for this session.")
    elif triage_id:
        lines.append("Saved to your triage intake. Send TELEHEALTH to book a video consult.")
    lines.append("Add a caption next time to help our clinical review.")
    return "\n\n".join(lines)


def notify_provider_media(*, attachment_id: str, telehealth_session_id: str) -> None:
    with session_scope() as s:
        att = s.get(PatientMediaAttachment, attachment_id)
        session = s.get(TelehealthSession, telehealth_session_id)
        if not att or not session:
            return
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider or not (provider.contact_phone_e164 or "").strip():
            return
        dest = normalize_phone_e164(provider.contact_phone_e164)
        patient = (
            mask_phone_for_display(session.patient_phone_e164, session_id=session.id)
            if session.mask_patient_number
            else session.patient_phone_e164
        )
        base = (get_effective_integration().hc_base_url or "").rstrip("/")
        admin_url = f"{base}/admin/patient-media-attachment/details/{attachment_id}"
        body = (
            f"Patient media for telehealth {telehealth_session_id.replace('ths_', 'TH-')[:16].upper()}\n"
            f"Patient: {patient}\n"
            f"Type: {att.media_type} / {att.media_category or '—'}\n"
            f"Caption: {(att.caption or '—')[:200]}\n"
            f"Review: {admin_url}"
        )
    res = send_whatsapp_broadcast(to_phone_e164=dest, body=body)
    if not res.ok:
        send_message(to_phone_e164=dest, body=body, channel="sms")


def _notify_operator_media(
    *,
    attachment_id: str,
    phone_e164: str,
    triage_session_id: Optional[str],
    ths_id: Optional[str],
) -> None:
    import requests

    url = (get_effective_integration().operator_webhook_url or "").strip()
    if not url:
        return
    base = (get_effective_integration().hc_base_url or "").rstrip("/")
    payload: dict[str, Any] = {
        "event": "media.received",
        "attachment_id": attachment_id,
        "phone_e164": phone_e164,
        "triage_session_id": triage_session_id,
        "telehealth_session_id": ths_id,
        "admin_url": f"{base}/admin/patient-media-attachment/details/{attachment_id}",
    }
    try:
        requests.post(url, json=payload, timeout=12)
    except Exception as exc:
        _log.warning("operator media webhook failed: %s", exc)
