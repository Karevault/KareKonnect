"""
KareVault Symptom Checker API — https://symptoms-checker.karevault.com/api/docs/

Uses either:
- Partner flow: POST /api/external/v1/request-access/ then POST /api/external/v1/chat/ (Bearer access_token)
- Public flow: POST /api/ai/chat/ (no auth; suitable for demos only)

Medical disease/symptom list endpoints require Token auth and may return an empty catalogue; chat is the
primary integration for patient Q&A.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Namespace for deriving a stable per-consultation KareVault session id from our own
# triage_session_id, so the same consultation always maps to the same external session
# without needing a new database column.
_SESSION_ID_NAMESPACE = uuid.UUID("2f6a2b0e-6b8d-4d3a-9f0a-2a6a8c4d5e6f")


def karevault_session_id(triage_session_id: Optional[str]) -> Optional[str]:
    """Deterministic UUID for a given triage_session_id, or None if there isn't one."""
    if not triage_session_id:
        return None
    return str(uuid.uuid5(_SESSION_ID_NAMESPACE, triage_session_id))

_lock = threading.Lock()
_partner_access_token: Optional[str] = None
_partner_token_expires_at: float = 0.0

_PROGRAM_CONTEXT: dict[str, str] = {
    "maternal_child": "Context: maternal, pregnancy, newborn, or postpartum care.",
    "chronic": "Context: chronic disease self-management and follow-up.",
    "youth": "Context: adolescent and youth health.",
    "kidney": "Context: kidney disease or dialysis.",
}


def _truthy_env(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def karevault_enabled() -> bool:
    return _truthy_env("HC_KAREVAULT_ENABLED")


def _base_url() -> str:
    u = (os.getenv("HC_KAREVAULT_BASE_URL") or "").strip()
    return u or "https://symptoms-checker.karevault.com"


def _base() -> str:
    return _base_url().rstrip("/")


def _use_partner() -> bool:
    return bool((os.getenv("HC_KAREVAULT_PARTNER_EMAIL") or "").strip())


def _use_public_chat() -> bool:
    """Use unauthenticated /api/ai/chat/ when no partner email (demo / low-friction)."""
    return not _use_partner()


_ASSESSMENT_COMPLETE_MARKERS = (
    "completed your medical assessment",
    "completed your assessment",
    "primary diagnosis:",
)


def assessment_is_complete(reply: Optional[str]) -> bool:
    """True when KareVault has delivered its final assessment, so the consultation is over."""
    text = (reply or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _ASSESSMENT_COMPLETE_MARKERS)


def build_contextual_question(
    question: str, programs: list[str], *, new_consultation: bool = False
) -> str:
    q = (question or "").strip()[:1200]
    bits = [_PROGRAM_CONTEXT[p] for p in programs if p in _PROGRAM_CONTEXT]
    ctx = " ".join(bits) if bits else "Context: general health education patient (programme not specified)."
    reset = (
        "This is a NEW consultation. Discard every earlier message, symptom, and assessment from this "
        "conversation and start over using only the patient message below. "
        if new_consultation
        else ""
    )
    return (
        f"{ctx} You support an SMS/WhatsApp health education service. "
        f"{reset}"
        "Anchor your reply to the symptom or concern the patient describes below. "
        "Reply in 2-4 short sentences — practical and direct. "
        "Clearly say when to seek urgent in-person or emergency care. "
        "Do not give specific drug doses. "
        "Patient message:\n"
        f"{q}"
    )


def _get_partner_access_token() -> Optional[str]:
    global _partner_access_token, _partner_token_expires_at
    email = (os.getenv("HC_KAREVAULT_PARTNER_EMAIL") or "").strip()
    name = (os.getenv("HC_KAREVAULT_PARTNER_NAME") or "Kareconnect").strip()
    if not email:
        return None
    now = time.monotonic()
    with _lock:
        if _partner_access_token and now < _partner_token_expires_at - 60:
            return _partner_access_token
        url = f"{_base()}/api/external/v1/request-access/"
        try:
            r = requests.post(
                url,
                json={"email": email, "name": name},
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
            if not (200 <= r.status_code < 300):
                log.warning("KareVault request-access HTTP %s: %s", r.status_code, (r.text or "")[:400])
                return None
            data = r.json()
            tok = (data.get("access_token") or "").strip()
            if not tok:
                log.warning("KareVault request-access: no access_token in body")
                return None
            exp = float(data.get("expires_in", 3500))
            _partner_access_token = tok
            _partner_token_expires_at = now + max(300.0, exp)
            return _partner_access_token
        except Exception as exc:
            log.warning("KareVault request-access failed: %s", exc)
            return None


def fetch_symptom_checker_reply(
    *, question: str, programs: list[str], new_consultation: bool = False, session_id: Optional[str] = None
) -> Optional[str]:
    """
    Returns plain-text AI reply for the patient, or None if disabled / error.

    `session_id` scopes KareVault's own server-side conversation memory to this one
    consultation. Without it, every call from this app shares a single conversation on
    KareVault's side (there is no other per-user identifier in the request), so unrelated
    patients' symptoms and diagnoses bleed into each other. Callers should pass a stable
    id for the lifetime of one triage session and a new one for each new consultation.
    """
    if not karevault_enabled():
        return None
    message = build_contextual_question(question, programs, new_consultation=new_consultation)
    base = _base()
    try:
        if _use_partner():
            token = _get_partner_access_token()
            if not token:
                return None
            url = f"{base}/api/external/v1/chat/"
            body: dict[str, str] = {"message": message}
            if session_id:
                body["session_id"] = session_id
            r = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=30,
            )
        elif _use_public_chat():
            url = f"{base}/api/ai/chat/"
            body = {"message": message}
            if session_id:
                body["session_id"] = session_id
            r = requests.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
        else:
            return None

        if not (200 <= r.status_code < 300):
            log.warning("KareVault chat HTTP %s: %s", r.status_code, (r.text or "")[:400])
            return None
        data = r.json()
        text = (data.get("ai_response") or data.get("diagnosis") or "").strip()
        return text or None
    except Exception as exc:
        log.warning("KareVault chat failed: %s", exc)
        return None
