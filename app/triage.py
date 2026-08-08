"""Post-JOIN triage sessions — intake window for text, photos, and documents."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from sqlalchemy import func, select

from app.database import session_scope
from app.models import TelehealthSession, TriageSession, User
from app.util import new_id, normalize_phone_e164, now_iso, safe_json_dumps

Channel = Literal["sms", "whatsapp", "web"]
_INTAKE_AGE_STAGE = "ask_age_group"
_INTAKE_CONTEXT_STAGE = "ask_patient_context"
_INTAKE_COMPLAINT_STAGE = "ask_chief_complaint"
_INTAKE_COMPLETE_STAGE = "complete"


def _default_intake_profile() -> dict[str, str]:
    return {}


def _load_intake_profile(raw: Optional[str]) -> dict[str, str]:
    if not raw:
        return _default_intake_profile()
    try:
        data = json.loads(raw)
    except Exception:
        return _default_intake_profile()
    if not isinstance(data, dict):
        return _default_intake_profile()
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _age_group_prompt() -> str:
    return (
        "Before clinical assessment, tell us the age group:\n"
        "1. Under 5\n"
        "2. 5-17\n"
        "3. 18-49\n"
        "4. 50+"
    )


def _patient_context_prompt() -> str:
    return (
        "Which best describes the patient today?\n"
        "1. Pregnant\n"
        "2. Postpartum or newborn care\n"
        "3. Adult woman, not pregnancy-related\n"
        "4. Adult man\n"
        "5. Other or not sure"
    )


def _chief_complaint_prompt() -> str:
    return "What is the main health question or symptom today? Reply in one sentence."


def _normalize_choice(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_generic_greeting(text: str) -> bool:
    return bool(re.fullmatch(r"(hi|hello|hey|good morning|good afternoon|good evening|morning|afternoon|evening)", _normalize_choice(text)))


def _parse_age_group_choice(text: str) -> Optional[str]:
    choice = _normalize_choice(text)
    if choice in ("1", "under 5", "under five", "baby", "infant", "toddler"):
        return "under_5"
    if choice in ("2", "5-17", "5 to 17", "child", "teen", "teenager", "adolescent"):
        return "5_17"
    if choice in ("3", "18-49", "18 to 49", "adult", "18-49 adult"):
        return "18_49"
    if choice in ("4", "50+", "50 plus", "older adult", "elderly", "senior"):
        return "50_plus"
    return None


def _parse_patient_context_choice(text: str) -> Optional[str]:
    choice = _normalize_choice(text)
    if choice in ("1", "pregnant", "pregnancy"):
        return "pregnant"
    if choice in ("2", "postpartum", "newborn", "postpartum or newborn care", "after delivery"):
        return "postpartum_newborn"
    if choice in ("3", "woman", "adult woman", "female", "adult woman not pregnancy-related"):
        return "adult_woman"
    if choice in ("4", "man", "adult man", "male"):
        return "adult_man"
    if choice in ("5", "other", "not sure", "unsure"):
        return "other_or_unsure"
    return None


def current_intake_prompt(*, triage_session_id: Optional[str]) -> str:
    if not triage_session_id:
        return _age_group_prompt()
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        stage = row.intake_stage if row else _INTAKE_AGE_STAGE
        profile = _load_intake_profile(row.intake_profile_json if row else None)
    return _prompt_for_stage(stage, profile=profile)


def _prompt_for_stage(stage: str, *, profile: dict[str, str]) -> str:
    if stage == _INTAKE_CONTEXT_STAGE:
        return _patient_context_prompt()
    if stage == _INTAKE_COMPLAINT_STAGE:
        return _chief_complaint_prompt()
    if stage == _INTAKE_COMPLETE_STAGE:
        return ""
    return _age_group_prompt()


def _acknowledge_pending_complaint(text: str) -> str:
    preview = " ".join((text or "").strip().split())[:120]
    if not preview:
        return _age_group_prompt()
    return f"I noted that: {preview}\n\n{_age_group_prompt()}"


def _advance_intake_state(
    *,
    stage: str,
    profile: dict[str, str],
    response_text: str,
) -> tuple[str, dict[str, str], Optional[str], Optional[str]]:
    text = (response_text or "").strip()
    updated = dict(profile)

    if stage == _INTAKE_COMPLETE_STAGE:
        return stage, updated, None, None

    if stage == _INTAKE_AGE_STAGE:
        age_group = _parse_age_group_choice(text)
        if age_group:
            updated["age_group"] = age_group
            return _INTAKE_CONTEXT_STAGE, updated, _patient_context_prompt(), None
        if text and not _is_generic_greeting(text) and not updated.get("pending_complaint"):
            updated["pending_complaint"] = text
            return stage, updated, _acknowledge_pending_complaint(text), None
        return stage, updated, _age_group_prompt(), None

    if stage == _INTAKE_CONTEXT_STAGE:
        patient_context = _parse_patient_context_choice(text)
        if patient_context:
            updated["patient_context"] = patient_context
            pending = (updated.get("pending_complaint") or "").strip()
            if pending:
                updated.pop("pending_complaint", None)
                return _INTAKE_COMPLETE_STAGE, updated, None, pending
            return _INTAKE_COMPLAINT_STAGE, updated, _chief_complaint_prompt(), None
        return stage, updated, _patient_context_prompt(), None

    if stage == _INTAKE_COMPLAINT_STAGE:
        if not text:
            return stage, updated, _chief_complaint_prompt(), None
        return _INTAKE_COMPLETE_STAGE, updated, None, text

    return _INTAKE_AGE_STAGE, updated, _age_group_prompt(), None


def process_triage_onboarding(
    *,
    triage_session_id: str,
    response_text: str,
) -> tuple[Optional[str], Optional[str]]:
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        if not row:
            return _age_group_prompt(), None
        profile = _load_intake_profile(row.intake_profile_json)
        next_stage, next_profile, reply, chief_complaint = _advance_intake_state(
            stage=row.intake_stage or _INTAKE_AGE_STAGE,
            profile=profile,
            response_text=response_text,
        )
        row.intake_stage = next_stage
        row.intake_profile_json = safe_json_dumps(next_profile) if next_profile else None
        if chief_complaint:
            row.chief_complaint = chief_complaint[:2000]
            row.intake_completed_at = now_iso()
            row.updated_at = row.intake_completed_at
        else:
            row.updated_at = now_iso()
        return reply, chief_complaint


def intake_complete(*, triage_session_id: Optional[str]) -> bool:
    if not triage_session_id:
        return False
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        if not row:
            return False
        return (row.intake_stage or "") == _INTAKE_COMPLETE_STAGE


def assessment_program_context(
    *,
    programs: list[str],
    triage_session_id: Optional[str],
    chief_complaint: Optional[str] = None,
) -> list[str]:
    if "maternal_child" not in programs or not triage_session_id:
        return list(programs)
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        profile = _load_intake_profile(row.intake_profile_json if row else None)
        stored_complaint = (row.chief_complaint or "") if row else ""
    patient_context = (profile.get("patient_context") or "").strip()
    combined = f"{stored_complaint} {chief_complaint or ''}".lower()
    if patient_context in ("pregnant", "postpartum_newborn"):
        return list(programs)
    if re.search(r"\b(pregnan|postpartum|newborn|baby|fetal|antenatal|delivery)\b", combined, re.I):
        return list(programs)
    return [p for p in programs if p != "maternal_child"]


def triage_window_hours() -> int:
    raw = (os.getenv("HC_TRIAGE_WINDOW_HOURS") or "72").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 72


def _expires_at_iso() -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=triage_window_hours())
    return dt.replace(microsecond=0).isoformat()


def start_triage_session(
    *,
    phone_e164: str,
    channel: Channel,
    user_id: Optional[str] = None,
) -> str:
    now = now_iso()
    with session_scope() as s:
        if not user_id:
            user_id = s.execute(select(User.id).where(User.phone_e164 == phone_e164)).scalar_one_or_none()
        active = (
            s.execute(
                select(TriageSession)
                .where(
                    TriageSession.phone_e164 == phone_e164,
                    TriageSession.status == "active",
                )
                .order_by(TriageSession.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if active:
            active.expires_at = _expires_at_iso()
            active.updated_at = now
            return active.id
        tid = new_id("tri")
        s.add(
            TriageSession(
                id=tid,
                user_id=user_id,
                phone_e164=phone_e164,
                channel=channel,
                status="active",
                intake_stage=_INTAKE_AGE_STAGE,
                expires_at=_expires_at_iso(),
                created_at=now,
                updated_at=now,
            )
        )
        return tid


def get_active_triage_session(phone_e164: str) -> Optional[TriageSession]:
    now = now_iso()
    with session_scope() as s:
        row = (
            s.execute(
                select(TriageSession)
                .where(
                    TriageSession.phone_e164 == phone_e164,
                    TriageSession.status == "active",
                )
                .order_by(TriageSession.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if not row:
            return None
        if row.expires_at and row.expires_at < now:
            row.status = "closed"
            row.closed_at = now
            row.updated_at = now
            return None
        s.expunge(row)
        return row


def complete_triage_session(triage_session_id: Optional[str]) -> bool:
    """
    End the consultation once the assessment has been delivered.

    Marked "completed" rather than "closed" so the next inbound message can be greeted
    with the main menu exactly once; consume_completed_consultation() then closes it.
    """
    if not triage_session_id:
        return False
    now = now_iso()
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        if not row or row.status != "active":
            return False
        row.status = "completed"
        row.closed_at = now
        row.updated_at = now
        return True


def consume_completed_consultation(phone_e164: str) -> bool:
    """
    True once after a consultation finished, so the caller can show the welcome menu.

    Flips the session to "closed" so the greeting is not repeated on later messages.
    """
    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    consumed = False
    with session_scope() as s:
        # created_at only has second precision, so match on status instead of ordering:
        # at most one consultation is awaiting acknowledgement at any time.
        rows = (
            s.execute(
                select(TriageSession).where(
                    TriageSession.phone_e164 == phone,
                    TriageSession.status == "completed",
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "closed"
            row.updated_at = now
            consumed = True
    return consumed


def cancel_active_triage_session(phone_e164: str) -> bool:
    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    changed = False
    with session_scope() as s:
        rows = (
            s.execute(
                select(TriageSession).where(
                    TriageSession.phone_e164 == phone,
                    TriageSession.status == "active",
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "closed"
            row.closed_at = now
            row.updated_at = now
            changed = True
    return changed


def get_active_telehealth_session_id(phone_e164: str) -> Optional[str]:
    with session_scope() as s:
        row = (
            s.execute(
                select(TelehealthSession.id)
                .where(
                    TelehealthSession.patient_phone_e164 == phone_e164,
                    TelehealthSession.status.in_(["confirmed", "in_progress"]),
                )
                .order_by(TelehealthSession.created_at.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        return row


def count_intake_assessments(*, phone_e164: str, triage_session_id: Optional[str] = None) -> int:
    """How many clinical assessments already exist for this intake (before the current message)."""
    from app.models import TriageAssessment

    with session_scope() as s:
        q = select(func.count()).select_from(TriageAssessment).where(
            TriageAssessment.phone_e164 == phone_e164
        )
        if triage_session_id:
            q = q.where(TriageAssessment.triage_session_id == triage_session_id)
        return int(s.execute(q).scalar() or 0)


def intake_briefing_due(*, phone_e164: str, triage_session_id: Optional[str] = None) -> bool:
    """First patient-facing reply in this window includes triage details and command hints."""
    return count_intake_assessments(phone_e164=phone_e164, triage_session_id=triage_session_id) == 0


def triage_welcome_addon() -> str:
    return (
        "\n\nClinical triage starts with a short intake so we do not assume the wrong context. "
        "First we ask age group and whether this is pregnancy/postpartum, then we ask for the main symptom or question. "
        "You can also send photos (wound, rash, eye) and documents (lab results, prescriptions) on WhatsApp. "
        "We run preliminary assessment — not a diagnosis. Send TRIAGE for triage status."
    )


def triage_status_text(phone_e164: str) -> str:
    triage = get_active_triage_session(phone_e164)
    ths = get_active_telehealth_session_id(phone_e164)
    if not triage and not ths:
        return (
            "No active triage window. Send JOIN if enrolled, or TELEHEALTH to book a clinician. "
            "On WhatsApp you can still send photos with a short description."
        )
    lines = ["Triage status:"]
    if triage:
        lines.append(f"• Intake window open until {triage.expires_at[:16]} UTC")
        if triage.chief_complaint:
            lines.append(f"• Chief concern: {triage.chief_complaint[:120]}")
    if ths:
        lines.append(f"• Telehealth session active (ref {ths.replace('ths_', 'TH-')[:16].upper()})")
        lines.append("• You can send photos/documents here — they are shared with your clinician.")
    lines.append("Send images or PDF documents with a short caption describing what we are looking at.")
    return "\n".join(lines)


def set_chief_complaint(*, triage_session_id: str, text: str) -> None:
    with session_scope() as s:
        row = s.get(TriageSession, triage_session_id)
        if row:
            row.chief_complaint = text[:2000]
            row.intake_stage = _INTAKE_COMPLETE_STAGE
            row.intake_completed_at = now_iso()
            row.updated_at = now_iso()
