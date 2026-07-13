"""Per-patient next-of-kin contacts (local registry + optional EHR sync)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app.database import session_scope
from app.messaging import send_message, send_whatsapp_broadcast
from app.models import PatientNextOfKin, User
from app.util import new_id, now_iso, normalize_phone_e164

_log = logging.getLogger(__name__)


@dataclass
class NextOfKinContact:
    full_name: str
    phone_e164: str
    relationship: Optional[str]
    source: str  # local|ehr


def list_patient_next_of_kin(*, user_id: str) -> list[NextOfKinContact]:
    """All emergency contacts registered for this patient (not a shared global list)."""
    with session_scope() as s:
        rows = list(
            s.execute(
                select(PatientNextOfKin)
                .where(PatientNextOfKin.user_id == user_id)
                .order_by(PatientNextOfKin.full_name.asc())
            )
            .scalars()
            .all()
        )
        return [
            NextOfKinContact(
                full_name=r.full_name,
                phone_e164=r.phone_e164,
                relationship=r.relationship,
                source="local",
            )
            for r in rows
        ]


def list_emergency_notify_contacts(*, user_id: str, phone_e164: str) -> list[NextOfKinContact]:
    """
    Contacts to notify on emergency: this patient's local NOK rows first,
    then any from EHR if none are configured locally.
    """
    with session_scope() as s:
        rows = list(
            s.execute(
                select(PatientNextOfKin).where(
                    PatientNextOfKin.user_id == user_id,
                    PatientNextOfKin.notify_on_emergency.is_(True),
                )
            )
            .scalars()
            .all()
        )
    if rows:
        return [
            NextOfKinContact(
                full_name=r.full_name,
                phone_e164=r.phone_e164,
                relationship=r.relationship,
                source="local",
            )
            for r in rows
        ]

    from app.ehr_client import fetch_next_of_kin_from_ehr, resolve_ehr_patient

    resolve_ehr_patient(user_id=user_id, phone_e164=phone_e164)
    ehr_contacts = fetch_next_of_kin_from_ehr(user_id=user_id)
    return [c for c in ehr_contacts if c.phone_e164]


def add_patient_next_of_kin(
    *,
    user_id: str,
    full_name: str,
    phone_e164: str,
    relationship: Optional[str] = None,
    notify_on_emergency: bool = True,
) -> str:
    now = now_iso()
    phone = normalize_phone_e164(phone_e164)
    nid = new_id("nok")
    with session_scope() as s:
        s.add(
            PatientNextOfKin(
                id=nid,
                user_id=user_id,
                full_name=full_name.strip(),
                phone_e164=phone,
                relationship=(relationship or "").strip() or None,
                notify_on_emergency=notify_on_emergency,
                created_at=now,
                updated_at=now,
            )
        )
    return nid


def sync_next_of_kin_from_ehr(*, user_id: str, contacts: list[NextOfKinContact]) -> int:
    """Import EHR next-of-kin into local table when patient has none (does not overwrite)."""
    if not contacts:
        return 0
    with session_scope() as s:
        existing = s.execute(
            select(PatientNextOfKin.id).where(PatientNextOfKin.user_id == user_id).limit(1)
        ).scalar_one_or_none()
        if existing:
            return 0
    added = 0
    for c in contacts:
        add_patient_next_of_kin(
            user_id=user_id,
            full_name=c.full_name,
            phone_e164=c.phone_e164,
            relationship=c.relationship,
            notify_on_emergency=True,
        )
        added += 1
    return added


def notify_patient_next_of_kin(
    *,
    user_id: Optional[str],
    patient_phone: str,
    message: str,
) -> int:
    """Send separate WhatsApp/SMS to each next-of-kin for this individual patient."""
    phone = normalize_phone_e164(patient_phone)
    if not user_id:
        with session_scope() as s:
            user_id = s.execute(select(User.id).where(User.phone_e164 == phone)).scalar_one_or_none()
    if not user_id:
        return 0

    patient_name = ""
    with session_scope() as s:
        user = s.get(User, user_id)
        if user and user.full_name:
            patient_name = user.full_name.strip()

    contacts = list_emergency_notify_contacts(user_id=user_id, phone_e164=phone)
    if not contacts:
        _log.info("no next-of-kin for patient user_id=%s", user_id)
        return 0

    label = patient_name or f"patient {phone}"
    sent = 0
    for c in contacts:
        dest = normalize_phone_e164(c.phone_e164)
        if dest == phone:
            continue
        rel = f" ({c.relationship})" if c.relationship else ""
        body = (
            f"Kareconnect emergency alert for {label}{rel}.\n\n"
            f"{message}\n\n"
            "This message was sent because you are listed as their next of kin."
        )
        r = send_whatsapp_broadcast(to_phone_e164=dest, body=body)
        if not r.ok:
            r = send_message(to_phone_e164=dest, body=body, channel="sms")
        if r.ok:
            sent += 1
    return sent
