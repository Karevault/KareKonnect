"""
KareVault EHR integration for Kareconnect.

Partner API (patient identity): https://ehr.karevault.com/api/docs#partner-api-kareconnect
WhatsApp Teleconsult API (clinical): https://ehr.karevault.com/api/docs#whatsapp-teleconsult
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from sqlalchemy import select

from app.database import session_scope
from app.models import EhrPatientLink, EhrSyncLog, Enrollment, User
from app.util import new_id, now_iso, safe_json_dumps

_log = logging.getLogger(__name__)

_DEFAULT_BASE = "https://ehr.karevault.com"


def _base_url() -> str:
    return (os.getenv("HC_EHR_API_BASE_URL") or _DEFAULT_BASE).strip().rstrip("/")


def _partner_api_key() -> Optional[str]:
    return (
        (os.getenv("HC_EHR_PARTNER_API_KEY") or os.getenv("HC_EHR_API_KEY") or "").strip() or None
    )


def _teleconsult_api_key() -> Optional[str]:
    return (
        (os.getenv("HC_EHR_TELECONSULT_API_KEY") or os.getenv("HC_EHR_API_KEY") or "").strip()
        or None
    )


def ehr_enabled() -> bool:
    """True when base URL and at least one API credential are configured."""
    return bool(_base_url()) and bool(_partner_api_key() or _teleconsult_api_key())


def ehr_config_status() -> dict[str, Any]:
    return {
        "base_url": _base_url(),
        "partner_api": bool(_partner_api_key()),
        "teleconsult_api": bool(_teleconsult_api_key()),
        "enabled": ehr_enabled(),
    }


@dataclass
class EhrPatientContext:
    external_patient_id: Optional[str] = None
    external_mrn: Optional[str] = None
    active_programs: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    medications: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _log_sync(
    *,
    user_id: Optional[str],
    direction: str,
    resource_type: str,
    resource_id: Optional[str],
    status: str,
    request: Optional[dict[str, Any]] = None,
    response: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    with session_scope() as s:
        s.add(
            EhrSyncLog(
                id=new_id("ehrlog"),
                user_id=user_id,
                direction=direction,
                resource_type=resource_type,
                resource_id=resource_id,
                status=status,
                request_json=safe_json_dumps(request) if request else None,
                response_json=safe_json_dumps(response) if response else None,
                error=(error or "")[:2000] or None,
                created_at=now_iso(),
            )
        )


def _auth_headers(*, api_key: Optional[str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _partner_headers() -> dict[str, str]:
    key = _partner_api_key()
    headers = _auth_headers(api_key=key)
    if key:
        headers["X-Api-Key"] = key
    return headers


def _teleconsult_headers() -> dict[str, str]:
    return _auth_headers(api_key=_teleconsult_api_key())


def _patient_id_int(external_patient_id: str) -> Optional[int]:
    try:
        return int(str(external_patient_id).strip())
    except (TypeError, ValueError):
        return None


def _parse_nok_row(row: dict[str, Any]) -> Optional["NextOfKinContact"]:
    from app.next_of_kin import NextOfKinContact

    phone = (row.get("phone_e164") or row.get("phone") or "").strip()
    name = (row.get("full_name") or row.get("name") or "").strip()
    if not phone or not name:
        return None
    return NextOfKinContact(
        full_name=name,
        phone_e164=phone,
        relationship=(row.get("relationship") or "").strip() or None,
        source="ehr",
    )


def _medication_names(meds: Any) -> list[str]:
    if not isinstance(meds, list):
        return []
    out: list[str] = []
    for m in meds:
        if isinstance(m, dict):
            name = (m.get("name") or m.get("drug_name") or "").strip()
            if name:
                out.append(name)
        elif m:
            out.append(str(m))
    return out


def _parse_teleconsult_context(data: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Map KareVault teleconsult context → conditions, meds, allergies, risk_factors."""
    patient = data.get("patient") or {}
    clinical = patient.get("clinical") or {}
    if isinstance(clinical, dict):
        conditions = clinical.get("diagnoses") or clinical.get("conditions") or []
        allergies = clinical.get("allergies") or []
        risk = clinical.get("risk_factors") or []
    else:
        conditions, allergies, risk = [], [], []

    meds = _medication_names(patient.get("medications"))
    return (
        [str(c) for c in conditions if c],
        meds,
        [str(a) for a in allergies if a],
        [str(r) for r in risk if r],
    )


def _normalize_glucose_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_val = row.get("value_mg_dl")
    if raw_val is None:
        raw_val = row.get("value")
    try:
        value = float(raw_val) if raw_val is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    unit = (row.get("unit") or "mg/dL").strip()
    if unit.lower() in ("mmol/l", "mmol") and value:
        value = round(value * 18.0182, 1)
    return {
        "value_mg_dl": value,
        "recorded_at": row.get("recorded_at") or row.get("observed_at") or "",
        "reading_type": row.get("reading_type") or row.get("source") or "unknown",
        "unit": "mg/dL",
        "source": row.get("source"),
    }


# --- Partner API (KareConnect): patient identity --------------------------------


def lookup_ehr_patient_by_phone(*, phone_e164: str) -> Optional[dict[str, Any]]:
    """GET /api/partners/patients/lookup?phone_e164=…"""
    if not _partner_api_key():
        return None
    url = f"{_base_url()}/api/partners/patients/lookup"
    try:
        resp = requests.get(
            url,
            headers=_partner_headers(),
            params={"phone_e164": phone_e164},
            timeout=20,
        )
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            _log.warning("EHR partner lookup HTTP %s", resp.status_code)
            return None
        data = resp.json() or {}
        if data.get("id") or data.get("patient_id"):
            return data
        return None
    except Exception as exc:
        _log.warning("EHR partner lookup failed: %s", exc)
        return None


def create_ehr_patient(
    *,
    user_id: str,
    phone_e164: str,
    full_name: Optional[str] = None,
    programs: Optional[list[str]] = None,
    language: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """POST /api/partners/patients — idempotent by phone or external_ref."""
    if not _partner_api_key():
        return None
    payload: dict[str, Any] = {
        "phone_e164": phone_e164,
        "source": "kareconnect",
        "external_ref": user_id,
    }
    if full_name:
        payload["full_name"] = full_name
    if programs:
        payload["programs"] = programs
    if language:
        payload["language"] = language
    url = f"{_base_url()}/api/partners/patients"
    try:
        resp = requests.post(url, headers=_partner_headers(), json=payload, timeout=25)
        if not (200 <= resp.status_code < 300):
            _log_sync(
                user_id=user_id,
                direction="push",
                resource_type="partner_patients",
                resource_id=None,
                status="failed",
                request=payload,
                error=f"HTTP {resp.status_code}",
            )
            return None
        data = resp.json() or {}
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_patients",
            resource_id=str(data.get("id") or data.get("patient_id") or ""),
            status="ok",
            request=payload,
            response=data,
        )
        return data
    except Exception as exc:
        _log.warning("EHR partner patient create failed: %s", exc)
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_patients",
            resource_id=None,
            status="failed",
            request=payload,
            error=str(exc),
        )
        return None


def fetch_next_of_kin_from_ehr(*, user_id: str) -> list["NextOfKinContact"]:
    """GET /api/partners/patients/{id}/next-of-kin"""
    from app.next_of_kin import NextOfKinContact

    if not _partner_api_key():
        return []
    with session_scope() as s:
        link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
    if not link:
        return []
    pid = _patient_id_int(link.external_patient_id)
    if pid is None:
        return []
    url = f"{_base_url()}/api/partners/patients/{pid}/next-of-kin"
    try:
        resp = requests.get(url, headers=_partner_headers(), timeout=20)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json() or {}
        rows = data if isinstance(data, list) else (data.get("next_of_kin") or data.get("contacts") or [])
        return [c for row in rows if isinstance(row, dict) and (c := _parse_nok_row(row))]
    except Exception as exc:
        _log.warning("EHR partner next-of-kin fetch failed: %s", exc)
        return []


def resolve_ehr_patient(
    *,
    user_id: str,
    phone_e164: str,
    full_name: Optional[str] = None,
    programs: Optional[list[str]] = None,
    language: Optional[str] = None,
) -> Optional[str]:
    """
    Ensure Kareconnect user ↔ EHR patient link:
    1. Existing local link
    2. Partner lookup by phone
    3. Partner create if missing
    """
    now = now_iso()
    with session_scope() as s:
        link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
        if link:
            link.last_synced_at = now
            link.updated_at = now
            return link.external_patient_id

    if not _partner_api_key():
        return None

    found = lookup_ehr_patient_by_phone(phone_e164=phone_e164)
    if not found:
        snap_name = full_name
        snap_lang = language
        with session_scope() as s:
            user_row = s.get(User, user_id)
            if user_row:
                if not snap_name:
                    snap_name = user_row.full_name
                if not snap_lang:
                    snap_lang = user_row.language
        found = create_ehr_patient(
            user_id=user_id,
            phone_e164=phone_e164,
            full_name=snap_name,
            programs=programs,
            language=snap_lang,
        )
    if not found:
        return None

    ext_id = str(found.get("id") or found.get("patient_id") or "").strip()
    mrn = (found.get("mrn") or found.get("external_mrn") or "").strip() or None
    if not ext_id:
        return None

    link_patient(user_id=user_id, external_patient_id=ext_id, external_mrn=mrn)
    with session_scope() as s:
        link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
        if link:
            link.last_synced_at = now
            link.updated_at = now

    nok_rows = found.get("next_of_kin") or found.get("next_of_kin_contacts") or []
    if isinstance(nok_rows, list) and nok_rows:
        from app.next_of_kin import sync_next_of_kin_from_ehr

        contacts = [c for row in nok_rows if (c := _parse_nok_row(row))]
        sync_next_of_kin_from_ehr(user_id=user_id, contacts=contacts)

    return ext_id


# --- Partner API: appointments ---------------------------------------------------


@dataclass
class EhrAvailabilitySlot:
    date: str
    available: bool
    slots_remaining: int = 0
    message: str = ""


def fetch_ehr_appointment_availability(
    *,
    date_iso: str,
    visit_type: str = "in_person",
    external_patient_id: Optional[str] = None,
) -> EhrAvailabilitySlot:
    """
    GET /api/partners/appointments/availability — clinic slot check.
    Returns available=True when the API is missing or unreachable (staff will confirm).
    """
    fallback = EhrAvailabilitySlot(
        date=date_iso,
        available=True,
        slots_remaining=0,
        message="Availability will be confirmed by clinic staff.",
    )
    if not _partner_api_key():
        return fallback
    params: dict[str, str] = {"date": date_iso, "visit_type": visit_type}
    if external_patient_id:
        params["patient_id"] = str(external_patient_id)
    url = f"{_base_url()}/api/partners/appointments/availability"
    try:
        resp = requests.get(url, headers=_partner_headers(), params=params, timeout=20)
        if resp.status_code == 404:
            return fallback
        if not (200 <= resp.status_code < 300):
            _log.warning("EHR appointment availability HTTP %s", resp.status_code)
            return fallback
        data = resp.json() or {}
        available = bool(data.get("available", data.get("has_slots", True)))
        remaining = int(data.get("slots_remaining") or data.get("slots") or 0)
        msg = (data.get("message") or "").strip()
        return EhrAvailabilitySlot(
            date=date_iso,
            available=available,
            slots_remaining=remaining,
            message=msg,
        )
    except Exception as exc:
        _log.warning("EHR appointment availability failed: %s", exc)
        return fallback


def create_ehr_appointment(
    *,
    user_id: Optional[str],
    appointment_id: str,
    visit_type: str,
    reason: str,
    phone_e164: str,
    preferred_date: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    profession: Optional[str] = None,
    provider_name: Optional[str] = None,
    telehealth_session_id: Optional[str] = None,
    external_patient_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """
    POST /api/partners/appointments — create appointment in KareVault EHR.
    Returns (ehr_appointment_id, sync_status).
    """
    pid_str = external_patient_id
    if not pid_str and user_id:
        with session_scope() as s:
            link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
            if link:
                pid_str = link.external_patient_id

    payload: dict[str, Any] = {
        "source": "kareconnect",
        "external_ref": appointment_id,
        "visit_type": visit_type,
        "reason": reason,
        "phone_e164": phone_e164,
        "channel": channel or "whatsapp",
    }
    if pid_str:
        payload["patient_id"] = pid_str
    if preferred_date:
        payload["preferred_date"] = preferred_date
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    if profession:
        payload["profession"] = profession
    if provider_name:
        payload["provider_name"] = provider_name
    if telehealth_session_id:
        payload["telehealth_session_id"] = telehealth_session_id

    if not _partner_api_key():
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_appointment",
            resource_id=appointment_id,
            status="skipped",
            request=payload,
            error="HC_EHR_PARTNER_API_KEY not set",
        )
        return None, "skipped"

    if not pid_str:
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_appointment",
            resource_id=appointment_id,
            status="skipped",
            request=payload,
            error="no linked EHR patient_id",
        )
        return None, "skipped"

    url = f"{_base_url()}/api/partners/appointments"
    try:
        resp = requests.post(url, headers=_partner_headers(), json=payload, timeout=25)
        if resp.status_code == 404:
            _log_sync(
                user_id=user_id,
                direction="push",
                resource_type="partner_appointment",
                resource_id=appointment_id,
                status="skipped",
                request=payload,
                error="EHR appointment API not available (404)",
            )
            return None, "skipped"
        data: dict[str, Any] = {}
        try:
            data = resp.json() or {}
        except ValueError:
            data = {"text": (resp.text or "")[:500]}
        ok = 200 <= resp.status_code < 300
        ehr_id = str(data.get("id") or data.get("appointment_id") or "").strip() or None
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_appointment",
            resource_id=ehr_id or appointment_id,
            status="ok" if ok else "failed",
            request=payload,
            response=data,
            error=None if ok else f"HTTP {resp.status_code}",
        )
        if ok and ehr_id:
            return ehr_id, "synced"
        if ok:
            return appointment_id, "synced"
        return None, "failed"
    except Exception as exc:
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type="partner_appointment",
            resource_id=appointment_id,
            status="failed",
            request=payload,
            error=str(exc),
        )
        return None, "failed"


def sync_appointment_to_ehr(appointment_id: str) -> str:
    """Load local appointment row and push to EHR. Returns ehr_sync_status."""
    from app.models import Appointment, TelehealthProvider

    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return "skipped"
        user_id = appt.user_id
        phone = appt.phone_e164
        visit_type = appt.visit_type
        reason = appt.reason
        preferred_date = appt.preferred_date
        scheduled_at = appt.scheduled_at
        profession = appt.profession
        channel = appt.channel
        th_session_id = appt.telehealth_session_id
        provider_name: Optional[str] = None
        if appt.provider_id:
            prov = s.get(TelehealthProvider, appt.provider_id)
            if prov:
                provider_name = ((prov.title or "") + " " + (prov.full_name or "")).strip()

    ehr_id, status = create_ehr_appointment(
        user_id=user_id,
        appointment_id=appointment_id,
        visit_type=visit_type,
        reason=reason,
        phone_e164=phone,
        preferred_date=preferred_date,
        scheduled_at=scheduled_at,
        profession=profession,
        provider_name=provider_name,
        telehealth_session_id=th_session_id,
        channel=channel,
    )
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if appt:
            appt.ehr_sync_status = status
            if ehr_id:
                appt.ehr_appointment_id = ehr_id
            appt.updated_at = now_iso()
    return status


def link_patient(*, user_id: str, external_patient_id: str, external_mrn: Optional[str] = None) -> str:
    now = now_iso()
    with session_scope() as s:
        row = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
        if row:
            row.external_patient_id = external_patient_id
            row.external_mrn = external_mrn
            row.updated_at = now
            return row.id
        link_id = new_id("ehrl")
        s.add(
            EhrPatientLink(
                id=link_id,
                user_id=user_id,
                external_patient_id=external_patient_id,
                external_mrn=external_mrn,
                created_at=now,
                updated_at=now,
            )
        )
        return link_id


# --- WhatsApp Teleconsult API: clinical read/write --------------------------------


def fetch_patient_context(*, phone_e164: str, ensure_linked: bool = True) -> EhrPatientContext:
    """Local enrolment + GET /api/external/patient/{id}/context."""
    ctx = EhrPatientContext()
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    user_lang: Optional[str] = None
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone_e164)).scalar_one_or_none()
        if not user:
            return ctx
        user_id = user.id
        user_name = user.full_name
        user_lang = user.language
        link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user.id)).scalar_one_or_none()
        programs = (
            s.execute(
                select(Enrollment.program).where(
                    Enrollment.user_id == user.id, Enrollment.status == "active"
                )
            )
            .scalars()
            .all()
        )
        ctx.active_programs = [str(p) for p in programs]
        if link:
            ctx.external_patient_id = link.external_patient_id
            ctx.external_mrn = link.external_mrn

    if ensure_linked and user_id and _partner_api_key() and not ctx.external_patient_id:
        ext = resolve_ehr_patient(
            user_id=user_id,
            phone_e164=phone_e164,
            full_name=user_name,
            programs=ctx.active_programs,
            language=user_lang,
        )
        if ext:
            ctx.external_patient_id = ext
            with session_scope() as s:
                link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
                if link:
                    ctx.external_mrn = link.external_mrn

    if not _teleconsult_api_key() or not ctx.external_patient_id:
        ctx.risk_factors = _local_risk_factors(ctx.active_programs)
        return ctx

    pid = _patient_id_int(ctx.external_patient_id)
    if pid is None:
        ctx.risk_factors = _local_risk_factors(ctx.active_programs)
        return ctx

    url = f"{_base_url()}/api/external/patient/{pid}/context"
    try:
        resp = requests.get(url, headers=_teleconsult_headers(), timeout=20)
        if not (200 <= resp.status_code < 300):
            _log_sync(
                user_id=user_id,
                direction="fetch",
                resource_type="teleconsult_context",
                resource_id=ctx.external_patient_id,
                status="failed",
                error=f"HTTP {resp.status_code}",
            )
            ctx.risk_factors = _local_risk_factors(ctx.active_programs)
            return ctx
        data = resp.json() or {}
        conditions, meds, allergies, risk = _parse_teleconsult_context(data)
        ctx.conditions = conditions
        ctx.medications = meds
        ctx.allergies = allergies
        ctx.risk_factors = risk or _local_risk_factors(ctx.active_programs)
        ctx.raw = data
        patient = data.get("patient") or {}
        if not ctx.external_mrn:
            demo = patient.get("demographics") or {}
            ctx.external_mrn = (demo.get("mrn") or patient.get("mrn") or "").strip() or None
        _log_sync(
            user_id=user_id,
            direction="fetch",
            resource_type="teleconsult_context",
            resource_id=ctx.external_patient_id,
            status="ok",
            response=data,
        )
    except Exception as exc:
        _log.warning("EHR teleconsult context fetch failed: %s", exc)
        _log_sync(
            user_id=user_id,
            direction="fetch",
            resource_type="teleconsult_context",
            resource_id=ctx.external_patient_id,
            status="failed",
            error=str(exc),
        )
        ctx.risk_factors = _local_risk_factors(ctx.active_programs)
    return ctx


def _local_risk_factors(programs: list[str]) -> list[str]:
    factors: list[str] = []
    if "chronic" in programs:
        factors.append("chronic_disease")
    if "kidney" in programs:
        factors.append("kidney_disease")
    return factors


def fetch_glucose_history(*, phone_e164: str, days: int = 30) -> list[dict[str, Any]]:
    """GET /api/external/patients/{id}/glucose-readings?days=30"""
    ctx = fetch_patient_context(phone_e164=phone_e164)
    if not _teleconsult_api_key() or not ctx.external_patient_id:
        return []
    pid = _patient_id_int(ctx.external_patient_id)
    if pid is None:
        return []
    url = f"{_base_url()}/api/external/patients/{pid}/glucose-readings"
    try:
        resp = requests.get(
            url,
            headers=_teleconsult_headers(),
            params={"days": min(max(days, 1), 365)},
            timeout=20,
        )
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json() or {}
        rows = data if isinstance(data, list) else (data.get("readings") or [])
        return [_normalize_glucose_row(r) for r in rows if isinstance(r, dict)]
    except Exception as exc:
        _log.warning("EHR teleconsult glucose fetch failed: %s", exc)
        return []


def fetch_medication_regimen(*, phone_e164: str) -> list[str]:
    ctx = fetch_patient_context(phone_e164=phone_e164)
    if ctx.medications:
        return list(ctx.medications)
    if not _teleconsult_api_key() or not ctx.external_patient_id:
        return []
    pid = _patient_id_int(ctx.external_patient_id)
    if pid is None:
        return []
    url = f"{_base_url()}/api/external/patients/{pid}/medication"
    try:
        resp = requests.get(url, headers=_teleconsult_headers(), timeout=20)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json() or {}
        return _medication_names(data.get("medications") or data.get("active_medications") or [])
    except Exception as exc:
        _log.warning("EHR teleconsult medications fetch failed: %s", exc)
    return []


def _build_incident_body(*, patient_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    severity = (payload.get("severity") or "warning").strip()
    if severity == "emergency":
        severity = "critical"
    incident_type = (payload.get("incident_type") or payload.get("type") or "clinical_incident").strip()
    if incident_type == "emergency_incident":
        incident_type = "emergency_triage"
    description = (
        payload.get("description")
        or payload.get("patient_text")
        or payload.get("notes")
        or "Kareconnect clinical incident"
    )
    occurred_at = payload.get("occurred_at") or payload.get("timestamp") or now_iso()
    metadata = dict(payload.get("metadata") or {})
    for key in (
        "assessment_id",
        "triage_session_id",
        "phone_e164",
        "external_patient_id",
        "mrn",
        "type",
    ):
        if payload.get(key) is not None and key not in metadata:
            metadata[key] = payload[key]
    return {
        "patient_id": patient_id,
        "incident_type": incident_type,
        "severity": severity,
        "description": str(description)[:4000],
        "occurred_at": occurred_at,
        "reported_by": payload.get("reported_by") or "kareconnect",
        "metadata": metadata,
    }


def _build_observation_body(*, patient_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    raw_type = (payload.get("observation_type") or payload.get("type") or "").strip()
    observed_at = payload.get("observed_at") or payload.get("timestamp") or now_iso()
    metadata = dict(payload.get("metadata") or {})
    for key, val in payload.items():
        if key in (
            "patient_id",
            "observation_type",
            "type",
            "value",
            "unit",
            "notes",
            "observed_at",
            "timestamp",
            "metadata",
            "external_patient_id",
        ):
            continue
        if val is not None:
            metadata[key] = val

    is_glucose = raw_type in ("glucose", "glucose_monitoring") or payload.get("reading_mg_dl") is not None
    if is_glucose:
        value = payload.get("value")
        if value is None:
            value = payload.get("reading_mg_dl")
        return {
            "patient_id": patient_id,
            "observation_type": "glucose",
            "value": str(value) if value is not None else "",
            "unit": payload.get("unit") or "mg/dL",
            "notes": (payload.get("notes") or "")[:2000],
            "observed_at": observed_at,
            "metadata": metadata,
        }

    obs_type = raw_type if raw_type and raw_type not in ("clinical_note",) else "triage_assessment"
    urgency = payload.get("urgency_tier") or payload.get("risk_level") or ""
    notes_parts = [
        payload.get("differential_notes") or "",
        payload.get("patient_text") or "",
    ]
    notes = "\n".join(p for p in notes_parts if p).strip()
    return {
        "patient_id": patient_id,
        "observation_type": obs_type,
        "value": str(urgency)[:64],
        "unit": "",
        "notes": notes[:4000],
        "observed_at": observed_at,
        "metadata": metadata,
    }


def push_resource(
    *,
    user_id: Optional[str],
    resource_type: str,
    payload: dict[str, Any],
    resource_id: Optional[str] = None,
    external_patient_id: Optional[str] = None,
) -> bool:
    """
    Push to WhatsApp Teleconsult API:
    - incidents → POST /api/external/incidents
    - observations → POST /api/external/observation
    """
    if not _teleconsult_api_key():
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type=resource_type,
            resource_id=resource_id,
            status="skipped",
            request=payload,
            error="HC_EHR_TELECONSULT_API_KEY / HC_EHR_API_KEY not set",
        )
        return False

    pid_str = external_patient_id or payload.get("external_patient_id") or payload.get("patient_id")
    if not pid_str and user_id:
        with session_scope() as s:
            link = s.execute(select(EhrPatientLink).where(EhrPatientLink.user_id == user_id)).scalar_one_or_none()
            if link:
                pid_str = link.external_patient_id
    pid = _patient_id_int(str(pid_str or ""))
    if pid is None:
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type=resource_type,
            resource_id=resource_id,
            status="failed",
            request=payload,
            error="no linked EHR patient_id",
        )
        return False

    if resource_type in ("incidents", "incident"):
        url = f"{_base_url()}/api/external/incidents"
        body = _build_incident_body(patient_id=pid, payload=payload)
        log_type = "teleconsult_incidents"
    elif resource_type in ("observations", "observation"):
        url = f"{_base_url()}/api/external/observation"
        body = _build_observation_body(patient_id=pid, payload=payload)
        log_type = "teleconsult_observation"
    else:
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type=resource_type,
            resource_id=resource_id,
            status="failed",
            request=payload,
            error=f"unsupported resource_type: {resource_type}",
        )
        return False

    try:
        resp = requests.post(url, json=body, headers=_teleconsult_headers(), timeout=25)
        ok = 200 <= resp.status_code < 300
        response_body: dict[str, Any] = {}
        try:
            response_body = resp.json() or {}
        except ValueError:
            response_body = {"text": (resp.text or "")[:500]}
        ok = ok and response_body.get("ok", True)
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type=log_type,
            resource_id=resource_id or str(response_body.get("id") or ""),
            status="ok" if ok else "failed",
            request=body,
            response=response_body,
            error=None if ok else f"HTTP {resp.status_code}",
        )
        return ok
    except Exception as exc:
        _log_sync(
            user_id=user_id,
            direction="push",
            resource_type=log_type,
            resource_id=resource_id,
            status="failed",
            request=body,
            error=str(exc),
        )
        return False


def log_emergency_incident(
    *,
    user_id: Optional[str],
    phone_e164: str,
    assessment_id: Optional[str],
    patient_text: str,
    triage_session_id: Optional[str],
    timestamp: str,
) -> Optional[str]:
    ctx = fetch_patient_context(phone_e164=phone_e164)
    payload: dict[str, Any] = {
        "type": "emergency_incident",
        "incident_type": "emergency_triage",
        "severity": "critical",
        "description": patient_text[:2000],
        "occurred_at": timestamp,
        "phone_e164": phone_e164,
        "assessment_id": assessment_id,
        "triage_session_id": triage_session_id,
        "reported_by": "kareconnect",
    }
    if ctx.external_patient_id:
        payload["external_patient_id"] = ctx.external_patient_id
        payload["mrn"] = ctx.external_mrn
    if push_resource(
        user_id=user_id,
        resource_type="incidents",
        payload=payload,
        resource_id=assessment_id,
        external_patient_id=ctx.external_patient_id,
    ):
        return assessment_id or f"inc-{timestamp[:19]}"
    with session_scope() as s:
        s.add(
            EhrSyncLog(
                id=new_id("ehrlog"),
                user_id=user_id,
                direction="push",
                resource_type="emergency_incident_local",
                resource_id=assessment_id,
                status="ok",
                request_json=safe_json_dumps(payload),
                created_at=timestamp,
            )
        )
    return assessment_id or f"inc-local-{timestamp[:19]}"


def test_ehr_connectivity() -> dict[str, Any]:
    """Lightweight config check for /healthz/clinical (no patient data required)."""
    status = ehr_config_status()
    status["reachable"] = False
    if not status["enabled"]:
        return status
    # Partner key presence is enough for identity; teleconsult for clinical.
    try:
        resp = requests.get(
            f"{_base_url()}/api/v1/health",
            timeout=10,
        )
        status["reachable"] = resp.status_code < 500
        status["health_http"] = resp.status_code
    except Exception as exc:
        status["health_error"] = str(exc)[:200]
    return status
