"""
KareVault Telehealth API — WhatsApp doctor directory, availability, and appointment booking.

https://telehealthapi.karevault.com/api/integrations/whatsapp/*

Distinct from app/karevault_client.py (symptoms-checker.karevault.com, patient Q&A) and
app/ehr_client.py (ehr.karevault.com, clinical record sync) — this is the telehealth
scheduling/booking system behind the TELEHEALTH and APPOINT (video) WhatsApp flows.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_DEFAULT_BASE = "https://telehealthapi.karevault.com"
_DOCTORS_CACHE_TTL_SECONDS = 300  # avoid refetching on every message within one booking flow


def _base_url() -> str:
    base = (os.getenv("HC_TELEHEALTH_API_BASE_URL") or _DEFAULT_BASE).strip().rstrip("/")
    if base.lower().endswith("/api"):
        base = base[:-4].rstrip("/")
    return base


def _patient_call_url(appointment_id: Optional[int]) -> Optional[str]:
    """Build the documented patient portal URL when the API omits call_url."""
    if appointment_id is None:
        return None
    portal = (os.getenv("HC_TELEHEALTH_PATIENT_PORTAL_BASE_URL") or "https://telehealthportal.karevault.com").strip().rstrip("/")
    return f"{portal}/call/patient?id={appointment_id}"


def _token() -> Optional[str]:
    return (os.getenv("HC_TELEHEALTH_WHATSAPP_TOKEN") or "").strip() or None


def telehealth_api_enabled() -> bool:
    """True when base URL + bearer token are configured."""
    return bool(_base_url()) and bool(_token())


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


@dataclass(frozen=True)
class RemoteDoctor:
    id: int
    name: str
    specialty: str
    consultation_fee: int


@dataclass(frozen=True)
class RemoteSlot:
    date: str  # "2026-08-25"
    start: str  # "14:00"
    end: str  # "14:30"


@dataclass(frozen=True)
class AppointmentResult:
    ok: bool
    appointment_id: Optional[int] = None
    status: Optional[str] = None
    call_url: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RescheduleResult:
    ok: bool
    appointment_id: Optional[int] = None
    status: Optional[str] = None
    call_url: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


def _parse_fee(raw) -> int:
    """consultation_fee is returned as a decimal string ("10000.00"), not a bare int."""
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


_CALL_URL_KEYS = (
    "call_url",
    "meeting_url",
    "join_url",
    "video_url",
    "video_call_url",
    "meeting_link",
    "video_link",
    "video_call_link",
    "conference_url",
    "conference_link",
)


def _extract_call_url(data: Any) -> Optional[str]:
    """Read a meeting URL from current or nested telehealth API response shapes."""
    if not isinstance(data, dict):
        return None
    for key in _CALL_URL_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("appointment", "data", "result"):
        nested = _extract_call_url(data.get(key))
        if nested:
            return nested
    return None


def _extract_appointment_id(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    value = data.get("appointment_id")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    for key in ("appointment", "data", "result"):
        nested = _extract_appointment_id(data.get(key))
        if nested is not None:
            return nested
    return None


_lock = threading.Lock()
_doctors_cache: list[RemoteDoctor] = []
_doctors_cache_at: float = 0.0


def list_doctors(*, force_refresh: bool = False) -> list[RemoteDoctor]:
    """
    GET /api/integrations/whatsapp/doctors, cached for _DOCTORS_CACHE_TTL_SECONDS.
    Returns [] when disabled or on a cold-start failure; serves the stale cache on a
    transient error so a mid-flow patient's menu doesn't blank out over a network blip.
    """
    global _doctors_cache, _doctors_cache_at
    if not telehealth_api_enabled():
        return []
    now = time.monotonic()
    with _lock:
        if not force_refresh and _doctors_cache and now < _doctors_cache_at + _DOCTORS_CACHE_TTL_SECONDS:
            return _doctors_cache
    url = f"{_base_url()}/api/integrations/whatsapp/doctors"
    try:
        r = requests.get(url, headers=_headers(), timeout=20)
        if not (200 <= r.status_code < 300):
            log.warning("KareVault telehealth list_doctors HTTP %s: %s", r.status_code, (r.text or "")[:400])
            return _doctors_cache
        data = r.json()
        out = [
            RemoteDoctor(
                id=int(d["id"]),
                name=str(d.get("name") or "").strip(),
                specialty=str(d.get("specialty") or "").strip() or "General",
                consultation_fee=_parse_fee(d.get("consultation_fee")),
            )
            for d in data
            if d.get("id") is not None and str(d.get("name") or "").strip()
        ]
        with _lock:
            _doctors_cache = out
            _doctors_cache_at = now
        return out
    except Exception as exc:
        log.warning("KareVault telehealth list_doctors failed: %s", exc)
        return _doctors_cache


def invalidate_doctors_cache() -> None:
    global _doctors_cache, _doctors_cache_at
    with _lock:
        _doctors_cache = []
        _doctors_cache_at = 0.0


def fetch_doctor_availability(doctor_id: int, *, days: int = 7) -> list[RemoteSlot]:
    """GET /api/integrations/whatsapp/doctors/{doctor_id}/availability?days=N. Not cached."""
    if not telehealth_api_enabled():
        return []
    url = f"{_base_url()}/api/integrations/whatsapp/doctors/{doctor_id}/availability"
    try:
        r = requests.get(url, headers=_headers(), params={"days": days}, timeout=20)
        if not (200 <= r.status_code < 300):
            log.warning("KareVault telehealth availability HTTP %s: %s", r.status_code, (r.text or "")[:400])
            return []
        data = r.json()
        return [
            RemoteSlot(date=str(s["date"]), start=str(s["start"]), end=str(s["end"]))
            for s in data
            if s.get("date") and s.get("start") and s.get("end")
        ]
    except Exception as exc:
        log.warning("KareVault telehealth availability failed doctor=%s: %s", doctor_id, exc)
        return []


def create_appointment(
    *,
    first_name: str,
    last_name: str,
    phone: str,
    email: str,
    gender: str,
    dob: str,
    doctor_id: int,
    appointment_date: str,
    appointment_time: str,
    appointment_type: str = "video",
    reason: str = "",
) -> AppointmentResult:
    """POST /api/integrations/whatsapp/appointments."""
    if not telehealth_api_enabled():
        return AppointmentResult(ok=False, error="KareVault telehealth API not configured")
    payload = {
        "patient": {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "email": email,
            "gender": gender,
            "dob": dob,
        },
        "doctor_id": doctor_id,
        "appointment_date": appointment_date,
        "appointment_time": appointment_time,
        "appointment_type": appointment_type,
        "reason": (reason or "")[:500],
    }
    url = f"{_base_url()}/api/integrations/whatsapp/appointments"
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=30)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not (200 <= r.status_code < 300) or not data.get("success"):
            err = str(data.get("message") or data.get("detail") or f"HTTP {r.status_code}")
            log.warning(
                "KareVault create_appointment failed url=%s status=%s content_type=%s: %s — %s",
                url,
                r.status_code,
                r.headers.get("Content-Type"),
                err,
                (r.text or "")[:400],
            )
            return AppointmentResult(ok=False, error=err)
        appointment_id = _extract_appointment_id(data)
        call_url = _extract_call_url(data) or _patient_call_url(appointment_id)
        if not call_url:
            log.warning(
                "KareVault create_appointment succeeded without a meeting URL; response keys=%s",
                sorted(data.keys()),
            )
        return AppointmentResult(
            ok=True,
            appointment_id=appointment_id,
            status=data.get("status"),
            call_url=call_url,
        )
    except Exception as exc:
        log.warning("KareVault create_appointment exception: %s", exc)
        return AppointmentResult(ok=False, error=str(exc))


def reschedule_appointment(
    appointment_id: int,
    *,
    new_date: str,
    new_time: str,
    patient_phone: str,
) -> RescheduleResult:
    """POST /api/integrations/whatsapp/appointments/{appointment_id}/reschedule."""
    if not telehealth_api_enabled():
        return RescheduleResult(ok=False, error="KareVault telehealth API not configured")
    url = f"{_base_url()}/api/integrations/whatsapp/appointments/{appointment_id}/reschedule"
    payload = {"new_date": new_date, "new_time": new_time, "patient_phone": patient_phone}
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=30)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not (200 <= r.status_code < 300):
            err = str(data.get("message") or data.get("detail") or f"HTTP {r.status_code}")
            log.warning("KareVault reschedule_appointment failed: %s — %s", err, (r.text or "")[:400])
            return RescheduleResult(ok=False, error=err)
        return RescheduleResult(
            ok=True,
            appointment_id=_extract_appointment_id(data),
            status=data.get("status"),
            call_url=_extract_call_url(data),
            message=data.get("message"),
        )
    except Exception as exc:
        log.warning("KareVault reschedule_appointment exception: %s", exc)
        return RescheduleResult(ok=False, error=str(exc))
