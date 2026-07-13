"""Executable clinical action paths — emergency response and glucose monitoring."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.database import session_scope
from app.ehr_client import (
    EhrPatientContext,
    fetch_medication_regimen,
    fetch_patient_context,
    log_emergency_incident,
    push_resource,
)
from app.glucose_triage import (
    GlucoseCarePlan,
    GlucoseTrendAnalysis,
    analyze_30_day_trend,
    dietary_guidance_for_trend,
    draft_medication_recommendations,
    extract_glucose_from_text,
    store_glucose_reading,
)
from app.integration_config import get_effective_integration
from app.messaging import send_message, send_whatsapp_broadcast
from app.models import (
    ClinicalReminder,
    MedicationAdjustmentDraft,
    TelehealthProvider,
    TelehealthSession,
    User,
)
from app.next_of_kin import notify_patient_next_of_kin
from app.services import request_escalation
from app.telehealth import mask_phone_for_display, notify_provider_new_booking
from app.util import new_id, now_iso, normalize_phone_e164, safe_json_dumps

_log = logging.getLogger(__name__)


def emergency_hotline_text() -> str:
    return (os.getenv("HC_EMERGENCY_HOTLINE") or "122").strip()


def execute_emergency_action_path(
    *,
    phone_e164: str,
    user_id: Optional[str],
    assessment_id: Optional[str],
    patient_text: str,
    triage_session_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Emergency path: 122 instructions, on-call alert, next-of-kin, EHR incident, urgent teleconsult.
    Returns actions_taken and immediate patient message body.
    """
    phone = normalize_phone_e164(phone_e164)
    ehr = fetch_patient_context(phone_e164=phone)
    hotline = emergency_hotline_text()
    actions: list[str] = []
    now = now_iso()

    patient_msg = (
        f"EMERGENCY FLAG\n\n"
        f"Call {hotline} or 112 now if you are in immediate danger.\n"
        "Go to the nearest hospital — do not wait only for a chat reply.\n\n"
        "Our on-call clinician and your emergency contacts are being alerted now."
    )
    res = send_message(to_phone_e164=phone, body=patient_msg, channel="whatsapp")
    if res.ok:
        actions.append("send_emergency_patient_instructions")

    esc_id = request_escalation(
        from_phone=phone,
        reason=patient_text[:1000] or "Emergency clinical triage",
        urgency="emergency",
    )
    actions.append(f"auto_escalate_emergency:{esc_id}")

    on_call = _on_call_clinician_phone()
    if on_call:
        record = _format_patient_record_summary(phone=phone, ehr=ehr, assessment_id=assessment_id, text=patient_text)
        alert = (
            f"EMERGENCY TRIAGE — on-call alert\n"
            f"Patient: {record}\n"
            f"Assessment: {assessment_id or '—'}\n"
            f"Reported: {patient_text[:400]}\n"
            f"Time (UTC): {now}\n"
            "Respond via WhatsApp or open admin triage assessments."
        )
        r = send_whatsapp_broadcast(to_phone_e164=on_call, body=alert)
        if not r.ok:
            send_message(to_phone_e164=on_call, body=alert, channel="sms")
        actions.append("alert_on_call_clinician")

    nok_count = notify_patient_next_of_kin(
        user_id=user_id,
        patient_phone=phone,
        message=(
            f"They triggered an emergency health flag on Kareconnect. "
            f"Please check on them and ensure they call {hotline} or go to hospital if needed."
        ),
    )
    if nok_count:
        actions.append(f"notify_next_of_kin:{nok_count}")

    incident_id = log_emergency_incident(
        user_id=user_id,
        phone_e164=phone,
        assessment_id=assessment_id,
        patient_text=patient_text,
        triage_session_id=triage_session_id,
        timestamp=now,
    )
    if incident_id:
        actions.append(f"log_ehr_incident:{incident_id}")

    ths_id = _open_urgent_teleconsult_slot(
        phone_e164=phone,
        user_id=user_id,
        assessment_id=assessment_id,
        reason=patient_text,
    )
    if ths_id:
        actions.append(f"open_urgent_teleconsult:{ths_id}")

    return {"actions_taken": actions, "patient_message": patient_msg}


def execute_glucose_monitoring_path(
    *,
    phone_e164: str,
    user_id: Optional[str],
    ocr_text: Optional[str],
    caption: Optional[str],
    attachment_id: Optional[str],
    assessment_id: Optional[str] = None,
    triage_session_id: Optional[str] = None,
) -> Optional[GlucoseCarePlan]:
    """
    Routine glucose path: OCR extract → 30-day trend → EHR meds → draft recs → teleconsult → diet → reminder.
    """
    combined = f"{caption or ''}\n{ocr_text or ''}"
    extracted = extract_glucose_from_text(combined)
    if not extracted:
        return None

    store_glucose_reading(
        phone_e164=phone_e164,
        user_id=user_id,
        value_mg_dl=extracted.value_mg_dl,
        reading_type=extracted.reading_type,
        source="ocr",
        attachment_id=attachment_id,
        ocr_excerpt=extracted.raw_text,
    )

    trend = analyze_30_day_trend(phone_e164=phone_e164, user_id=user_id)
    meds = fetch_medication_regimen(phone_e164=phone_e164)
    recommendations = draft_medication_recommendations(trend=trend, medications=meds)
    dietary = dietary_guidance_for_trend(trend.trend)

    draft_id = _persist_medication_draft(
        user_id=user_id,
        phone_e164=phone_e164,
        assessment_id=assessment_id,
        triage_session_id=triage_session_id,
        trend=trend,
        medications=meds,
        recommendations=recommendations,
    )

    ths_id = _book_followup_teleconsult(
        phone_e164=phone_e164,
        user_id=user_id,
        profession="general_practice",
        priority="routine",
        reason=f"Glucose follow-up — {trend.summary}",
    )

    reminder_id = _schedule_glucose_reading_reminder(phone_e164=phone_e164, user_id=user_id)

    push_resource(
        user_id=user_id,
        resource_type="observations",
        payload={
            "type": "glucose_monitoring",
            "reading_mg_dl": extracted.value_mg_dl,
            "reading_type": extracted.reading_type,
            "trend": trend.trend,
            "plot_points": trend.plot_points,
            "draft_id": draft_id,
            "telehealth_session_id": ths_id,
        },
    )

    return GlucoseCarePlan(
        trend=trend,
        medication_regimen=meds,
        draft_recommendations=recommendations,
        dietary_guidance=dietary,
        telehealth_session_id=ths_id,
        draft_id=draft_id,
        reminder_id=reminder_id,
    )


def format_glucose_patient_reply(plan: GlucoseCarePlan, *, extracted_value: float, reading_type: str) -> str:
    lines = [
        f"Glucose reading recorded: {extracted_value:.0f} mg/dL ({reading_type.replace('_', ' ')}).",
        "",
        "30-day trend (preliminary):",
        plan.trend.summary,
    ]
    if plan.trend.plot_points:
        recent = plan.trend.plot_points[-5:]
        spark = ", ".join(f"{p['value_mg_dl']:.0f}" for p in recent)
        lines.append(f"Recent values (mg/dL): {spark}")
    lines.extend(
        [
            "",
            "Medication (draft for clinician approval only):",
            *[f"• {r}" for r in plan.draft_recommendations[:3]],
            "",
            plan.dietary_guidance,
        ]
    )
    if plan.telehealth_session_id:
        ref = plan.telehealth_session_id.replace("ths_", "TH-")[:16].upper()
        lines.append(f"\nFollow-up teleconsult queued (ref {ref}). We will confirm timing on this chat.")
    if plan.reminder_id:
        lines.append("Reminder scheduled for your next glucose reading.")
    lines.append("\n(Not a prescription — clinician must approve any medication change.)")
    return "\n".join(lines)


def _on_call_clinician_phone() -> Optional[str]:
    cfg = get_effective_integration()
    raw = (cfg.on_call_clinician_phone_e164 or cfg.helpdesk_phone_e164 or "").strip()
    if not raw:
        return None
    return normalize_phone_e164(raw)


def _format_patient_record_summary(
    *,
    phone: str,
    ehr: EhrPatientContext,
    assessment_id: Optional[str],
    text: str,
) -> str:
    parts = [phone]
    if ehr.external_mrn:
        parts.append(f"MRN {ehr.external_mrn}")
    if ehr.conditions:
        parts.append("Conditions: " + ", ".join(ehr.conditions[:4]))
    if ehr.medications:
        parts.append("Meds: " + ", ".join(ehr.medications[:4]))
    if assessment_id:
        parts.append(f"Assessment {assessment_id}")
    return " | ".join(parts)


def _open_urgent_teleconsult_slot(
    *,
    phone_e164: str,
    user_id: Optional[str],
    assessment_id: Optional[str],
    reason: str,
) -> Optional[str]:
    provider = _pick_provider(profession="general_practice")
    if not provider:
        return None
    now = now_iso()
    ths_id = new_id("ths")
    with session_scope() as s:
        s.add(
            TelehealthSession(
                id=ths_id,
                user_id=user_id,
                patient_phone_e164=phone_e164,
                provider_id=provider["id"],
                channel="whatsapp",
                profession_snapshot=provider["profession"],
                status="confirmed",
                fee_ngn=0,
                billing_status="waived",
                call_mode="whatsapp_video",
                priority="emergency",
                mask_patient_number=provider["mask_patient_number"],
                billing_notes=f"Urgent slot from emergency triage {assessment_id or ''}",
                created_at=now,
                updated_at=now,
            )
        )
    notify_provider_new_booking(ths_id, payment_received=True)
    return ths_id


def _book_followup_teleconsult(
    *,
    phone_e164: str,
    user_id: Optional[str],
    profession: str,
    priority: str,
    reason: str,
) -> Optional[str]:
    provider = _pick_provider(profession=profession)
    if not provider:
        return None
    now = now_iso()
    ths_id = new_id("ths")
    with session_scope() as s:
        s.add(
            TelehealthSession(
                id=ths_id,
                user_id=user_id,
                patient_phone_e164=phone_e164,
                provider_id=provider["id"],
                channel="whatsapp",
                profession_snapshot=provider["profession"],
                status="confirmed",
                fee_ngn=provider["fee_ngn"],
                billing_status="pending" if provider["fee_ngn"] > 0 else "waived",
                call_mode="whatsapp_video",
                priority=priority,
                mask_patient_number=provider["mask_patient_number"],
                billing_notes=reason[:500],
                created_at=now,
                updated_at=now,
            )
        )
    notify_provider_new_booking(ths_id, payment_received=False)
    return ths_id


def _pick_provider(*, profession: str) -> Optional[dict[str, Any]]:
    with session_scope() as s:
        row = (
            s.execute(
                select(TelehealthProvider)
                .where(TelehealthProvider.status == "active", TelehealthProvider.profession == profession)
                .order_by(TelehealthProvider.avg_rating.desc())
                .limit(1)
            )
            .scalars()
            .first()
        ) or (
            s.execute(
                select(TelehealthProvider)
                .where(TelehealthProvider.status == "active")
                .order_by(TelehealthProvider.avg_rating.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if not row:
            return None
        return {
            "id": row.id,
            "profession": row.profession,
            "fee_ngn": row.fee_ngn,
            "mask_patient_number": row.mask_patient_number,
        }


def _persist_medication_draft(
    *,
    user_id: Optional[str],
    phone_e164: str,
    assessment_id: Optional[str],
    triage_session_id: Optional[str],
    trend: GlucoseTrendAnalysis,
    medications: list[str],
    recommendations: list[str],
) -> str:
    did = new_id("medd")
    with session_scope() as s:
        s.add(
            MedicationAdjustmentDraft(
                id=did,
                user_id=user_id,
                phone_e164=phone_e164,
                assessment_id=assessment_id,
                triage_session_id=triage_session_id,
                trend_summary=trend.summary,
                current_regimen_json=safe_json_dumps(medications),
                draft_recommendations_json=safe_json_dumps(recommendations),
                trend_json=safe_json_dumps(
                    {
                        "trend": trend.trend,
                        "avg_mg_dl": trend.avg_mg_dl,
                        "post_meal_avg": trend.post_meal_avg,
                        "plot_points": trend.plot_points,
                    }
                ),
                status="pending_clinician",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    return did


def _schedule_glucose_reading_reminder(*, phone_e164: str, user_id: Optional[str]) -> str:
    due = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    rid = new_id("crm")
    body = (
        "Kareconnect reminder: please send your next glucose reading photo with caption "
        "'fasting' or 'after meal'. Reply STOP to pause programme messages."
    )
    with session_scope() as s:
        s.add(
            ClinicalReminder(
                id=rid,
                user_id=user_id,
                phone_e164=phone_e164,
                reminder_type="glucose_reading",
                due_at=due,
                message_body=body,
                status="scheduled",
                created_at=now_iso(),
            )
        )
        if user_id:
            from app.models import MessageOutbox, User

            user = s.get(User, user_id)
            if user:
                s.add(
                    MessageOutbox(
                        id=new_id("msg"),
                        user_id=user_id,
                        program="clinical_reminder",
                        channel="whatsapp",
                        language=user.language,
                        to_phone_e164=phone_e164,
                        body=body,
                        due_at=due,
                        status="queued",
                        created_at=now_iso(),
                    )
                )
    return rid
