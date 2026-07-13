"""Clinical reasoning engine — urgency, risk, protocol, medication safety, fraud detection."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.database import session_scope
from app.ehr_client import EhrPatientContext, fetch_patient_context, push_resource
from app.models import TriageAssessment
from app.util import new_id, now_iso, safe_json_dumps

_log = logging.getLogger(__name__)

_EMERGENCY_PATTERNS = re.compile(
    r"\b("
    r"chest pain|can't breathe|cannot breathe|difficulty breathing|severe bleeding|"
    r"unconscious|passed out|seizure|fit|fits|stroke|paralysis|"
    r"suicidal|kill myself|overdose|"
    r"severe headache|blurred vision|vision loss|"
    r"reduced baby movement|no fetal movement"
    r")\b",
    re.I,
)
_URGENT_PATTERNS = re.compile(
    r"\b("
    r"high fever|persistent fever|vomiting blood|blood in stool|"
    r"severe pain|worsening|swollen face|swelling face|"
    r"pregnant.{0,20}bleeding|antenatal bleeding"
    r")\b",
    re.I,
)
_REFILL_PATTERNS = re.compile(
    r"\b(refill|repeat prescription|more tablets|more pills|send medicine|prescribe)\b",
    re.I,
)
_NARCOTIC_PATTERNS = re.compile(
    r"\b(tramadol|codeine|morphine|oxycodone|pentazocine|rohypnol|diazepam|xanax)\b",
    re.I,
)


@dataclass
class ClinicalAssessmentResult:
    urgency_tier: str = "routine"
    risk_level: str = "low"
    protocol_id: str = "general_triage"
    medication_safety: str = "clear"
    fraud_signal: str = "clear"
    differential_notes: str = ""
    actions_taken: list[str] = field(default_factory=list)
    patient_suffix: str = ""
    assessment_id: Optional[str] = None


def clinical_intelligence_enabled() -> bool:
    v = (os.getenv("HC_CLINICAL_INTELLIGENCE_ENABLED") or "1").strip().lower()
    return v in ("1", "true", "yes", "on")


def load_action_paths() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "content" / "clinical_action_paths.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("clinical_action_paths load failed: %s", exc)
        return {}


def run_clinical_assessment(
    *,
    phone_e164: str,
    patient_text: Optional[str] = None,
    ocr_text: Optional[str] = None,
    vision_summaries: Optional[list[str]] = None,
    media_categories: Optional[list[str]] = None,
    programs: Optional[list[str]] = None,
    triage_session_id: Optional[str] = None,
    attachment_ids: Optional[list[str]] = None,
    source: str = "combined",
    user_id: Optional[str] = None,
    execute_actions: bool = True,
) -> ClinicalAssessmentResult:
    if not clinical_intelligence_enabled():
        return ClinicalAssessmentResult()

    text_blob = " ".join(
        t.strip()
        for t in [patient_text or "", ocr_text or "", " ".join(vision_summaries or [])]
        if t and t.strip()
    )
    ehr = fetch_patient_context(phone_e164=phone_e164)
    progs = programs or ehr.active_programs

    urgency = _assess_urgency(text_blob)
    risk = _assess_risk(text_blob, ehr=ehr, programs=progs)
    protocol = _match_protocol(text_blob, programs=progs, media_categories=media_categories or [])
    med = _assess_medication_safety(text_blob, ehr=ehr)
    fraud = _assess_fraud(text_blob, phone_e164=phone_e164)

    notes = _differential_notes(urgency=urgency, risk=risk, protocol=protocol, med=med, fraud=fraud)
    paths = load_action_paths()
    actions, suffix = _resolve_actions(
        paths=paths,
        urgency=urgency,
        risk=risk,
        protocol=protocol,
        med=med,
        fraud=fraud,
    )

    result = ClinicalAssessmentResult(
        urgency_tier=urgency,
        risk_level=risk,
        protocol_id=protocol,
        medication_safety=med,
        fraud_signal=fraud,
        differential_notes=notes,
        actions_taken=[],
        patient_suffix=suffix,
    )

    assessment_payload = {
        "urgency_tier": urgency,
        "risk_level": risk,
        "protocol_id": protocol,
        "medication_safety": med,
        "fraud_signal": fraud,
        "differential_notes": notes,
        "patient_text": (patient_text or "")[:2000],
        "triage_session_id": triage_session_id,
        "attachment_ids": attachment_ids or [],
        "ocr_text": (ocr_text or "")[:4000],
    }

    if execute_actions and urgency != "emergency":
        result.actions_taken = _execute_actions(
            actions=actions,
            phone_e164=phone_e164,
            user_id=user_id,
            urgency=urgency,
            assessment_payload=assessment_payload,
            ehr=ehr,
        )

    assessment_id = _persist_assessment(
        phone_e164=phone_e164,
        user_id=user_id,
        triage_session_id=triage_session_id,
        source=source,
        patient_text=patient_text,
        attachment_ids=attachment_ids,
        result=result,
        ehr_pushed=any(a.startswith("push_ehr") for a in result.actions_taken),
    )
    result.assessment_id = assessment_id

    if execute_actions and urgency == "emergency":
        from app.clinical_actions import execute_emergency_action_path

        outcome = execute_emergency_action_path(
            phone_e164=phone_e164,
            user_id=user_id,
            assessment_id=assessment_id,
            patient_text=(patient_text or ocr_text or "")[:2000],
            triage_session_id=triage_session_id,
        )
        result.actions_taken = outcome.get("actions_taken") or []
        if outcome.get("patient_message"):
            result.patient_suffix = str(outcome["patient_message"])

    return result


def format_assessment_for_patient(result: ClinicalAssessmentResult) -> str:
    if not result.assessment_id and result.urgency_tier == "routine" and result.risk_level == "low":
        return ""
    lines = [
        "Clinical triage (preliminary — not a diagnosis):",
        f"• Urgency: {result.urgency_tier}",
        f"• Risk: {result.risk_level}",
    ]
    if result.protocol_id and result.protocol_id != "general_triage":
        lines.append(f"• Pathway: {result.protocol_id.replace('_', ' ')}")
    if result.medication_safety != "clear":
        lines.append(f"• Medication review: {result.medication_safety.replace('_', ' ')}")
    if result.fraud_signal != "clear":
        lines.append("• Your request needs clinician review.")
    if result.patient_suffix:
        lines.append(result.patient_suffix)
    return "\n".join(lines)


def _assess_urgency(text: str) -> str:
    if _EMERGENCY_PATTERNS.search(text):
        return "emergency"
    if _URGENT_PATTERNS.search(text):
        return "urgent"
    return "routine"


def _assess_risk(text: str, *, ehr: EhrPatientContext, programs: list[str]) -> str:
    high_markers = (
        "pregnancy_or_postpartum" in ehr.risk_factors
        or "kidney_disease" in ehr.risk_factors
        or "diabetes" in " ".join(ehr.conditions).lower()
        or "hypertension" in " ".join(ehr.conditions).lower()
    )
    if high_markers and (_URGENT_PATTERNS.search(text) or _EMERGENCY_PATTERNS.search(text)):
        return "high"
    if high_markers or "chronic_disease" in ehr.risk_factors:
        return "moderate"
    if programs:
        return "moderate" if "maternal_child" in programs else "low"
    return "low"


def _match_protocol(text: str, *, programs: list[str], media_categories: list[str]) -> str:
    from app.glucose_triage import extract_glucose_from_text, is_glucose_related

    if is_glucose_related(text) or extract_glucose_from_text(text):
        return "glucose_monitoring"
    cats = set(media_categories)
    if "eye" in cats:
        return "ophthalmology_photo"
    if cats & {"wound", "rash", "skin"}:
        return "dermatology_photo"
    if "maternal_child" in programs and re.search(r"\b(pregnan|antenatal|baby|fetal|edd)\b", text, re.I):
        return "maternal_danger_signs"
    if "kidney" in programs:
        return "kidney_care"
    if "chronic" in programs:
        return "chronic_followup"
    return "general_triage"


def _assess_medication_safety(text: str, *, ehr: EhrPatientContext) -> str:
    if not _REFILL_PATTERNS.search(text):
        return "clear"
    meds = " ".join(ehr.medications).lower()
    if _NARCOTIC_PATTERNS.search(text) and _NARCOTIC_PATTERNS.search(meds):
        return "interaction_alert"
    if _REFILL_PATTERNS.search(text) and len(ehr.medications) >= 3:
        return "duplicate_therapy"
    if _REFILL_PATTERNS.search(text):
        return "interaction_alert"
    return "clear"


def _assess_fraud(text: str, *, phone_e164: str) -> str:
    if _NARCOTIC_PATTERNS.search(text) and _REFILL_PATTERNS.search(text):
        return "prescription_seeking"
    with session_scope() as s:
        from sqlalchemy import func, select

        recent_narcotic = s.execute(
            select(func.count(TriageAssessment.id)).where(
                TriageAssessment.phone_e164 == phone_e164,
                TriageAssessment.fraud_signal != "clear",
                TriageAssessment.created_at >= _days_ago_iso(30),
            )
        ).scalar_one()
    if int(recent_narcotic or 0) >= 2:
        return "repeat_narcotic"
    return "clear"


def _days_ago_iso(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat()


def _differential_notes(*, urgency: str, risk: str, protocol: str, med: str, fraud: str) -> str:
    parts = [f"Urgency={urgency}", f"Risk={risk}", f"Protocol={protocol}"]
    if med != "clear":
        parts.append(f"Medication={med}")
    if fraud != "clear":
        parts.append(f"Fraud={fraud}")
    return "; ".join(parts)


def _resolve_actions(
    *,
    paths: dict[str, Any],
    urgency: str,
    risk: str,
    protocol: str,
    med: str,
    fraud: str,
) -> tuple[list[str], str]:
    actions: list[str] = []
    suffix_parts: list[str] = []

    u = (paths.get("urgency_tiers") or {}).get(urgency) or {}
    actions.extend(u.get("actions") or [])
    if u.get("patient_suffix"):
        suffix_parts.append(str(u["patient_suffix"]))

    r = (paths.get("risk_levels") or {}).get(risk) or {}
    for a in r.get("actions") or []:
        if a not in actions:
            actions.append(a)
    if r.get("patient_suffix"):
        suffix_parts.append(str(r["patient_suffix"]))

    p = (paths.get("protocols") or {}).get(protocol) or {}
    for a in p.get("actions") or []:
        if a not in actions:
            actions.append(a)

    m = (paths.get("medication_safety") or {}).get(med) or {}
    for a in m.get("actions") or []:
        if a not in actions:
            actions.append(a)
    if m.get("patient_suffix"):
        suffix_parts.append(str(m["patient_suffix"]))

    f = (paths.get("fraud_abuse") or {}).get(fraud) or {}
    for a in f.get("actions") or []:
        if a not in actions:
            actions.append(a)
    if f.get("patient_suffix"):
        suffix_parts.append(str(f["patient_suffix"]))

    return actions, " ".join(suffix_parts).strip()


def _execute_actions(
    *,
    actions: list[str],
    phone_e164: str,
    user_id: Optional[str],
    urgency: str,
    assessment_payload: dict[str, Any],
    ehr: EhrPatientContext,
) -> list[str]:
    taken: list[str] = []
    for action in actions:
        try:
            if action == "auto_escalate_emergency":
                from app.services import request_escalation

                request_escalation(from_phone=phone_e164, reason=assessment_payload.get("patient_text") or "Emergency triage", urgency="emergency")
                taken.append(action)
            elif action == "auto_escalate_urgent":
                from app.services import request_escalation

                request_escalation(from_phone=phone_e164, reason=assessment_payload.get("patient_text") or "Urgent triage", urgency="urgent")
                taken.append(action)
            elif action.startswith("push_ehr"):
                payload = {**assessment_payload, "external_patient_id": ehr.external_patient_id}
                if push_resource(user_id=user_id, resource_type="observations", payload=payload):
                    taken.append(action)
            elif action == "notify_helpdesk":
                from app.services import open_helpdesk_ticket

                open_helpdesk_ticket(
                    from_phone=phone_e164,
                    channel="whatsapp",
                    question=f"Triage alert ({urgency}): {assessment_payload.get('patient_text', '')[:500]}",
                    suggested_answer=None,
                    ai_symptom_reply=assessment_payload.get("differential_notes"),
                )
                taken.append(action)
            else:
                taken.append(f"logged:{action}")
        except Exception as exc:
            _log.warning("clinical action %s failed: %s", action, exc)
            taken.append(f"failed:{action}")
    return taken


def _persist_assessment(
    *,
    phone_e164: str,
    user_id: Optional[str],
    triage_session_id: Optional[str],
    source: str,
    patient_text: Optional[str],
    attachment_ids: Optional[list[str]],
    result: ClinicalAssessmentResult,
    ehr_pushed: bool,
) -> str:
    aid = new_id("tria")
    with session_scope() as s:
        s.add(
            TriageAssessment(
                id=aid,
                triage_session_id=triage_session_id,
                user_id=user_id,
                phone_e164=phone_e164,
                source=source,
                patient_text=(patient_text or "")[:4000] or None,
                attachment_ids_json=safe_json_dumps(attachment_ids) if attachment_ids else None,
                urgency_tier=result.urgency_tier,
                risk_level=result.risk_level,
                protocol_id=result.protocol_id,
                medication_safety=result.medication_safety,
                fraud_signal=result.fraud_signal,
                differential_notes=result.differential_notes,
                actions_taken_json=safe_json_dumps(result.actions_taken),
                ehr_pushed=ehr_pushed,
                created_at=now_iso(),
            )
        )
    return aid
