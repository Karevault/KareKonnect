"""Telehealth: provider directory, patient booking flow, ratings, and billing."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional

import requests
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.africastalking import normalize_at_phone_number
from app.database import session_scope
from app.integration_config import get_effective_integration
from app.messaging import send_message, send_whatsapp_broadcast
from app.models import ProviderRating, TelehealthPatientFlow, TelehealthProvider, TelehealthSession, User
from app.util import new_id, now_iso, normalize_phone_e164

_log = logging.getLogger(__name__)

FlowStep = Literal["idle", "profession", "provider", "confirm"]

_OPEN_SESSION_STATUSES = ("confirmed", "in_progress")

PROFESSIONS: list[tuple[str, str]] = [
    ("gynaecologist", "Gynaecologist"),
    ("dialysis", "Dialysis / Nephrology"),
    ("general_practice", "General practice"),
    ("cardiology", "Cardiology"),
    ("paediatrics", "Paediatrics"),
    ("psychiatry", "Mental health"),
    ("dermatology", "Dermatology"),
    ("ophthalmology", "Ophthalmology"),
    ("other", "Other specialist"),
]

PROFESSION_LABELS = dict(PROFESSIONS)


def profession_label(code: str) -> str:
    return PROFESSION_LABELS.get((code or "").strip().lower(), code or "—")


def new_booking_session_status(fee_ngn: int) -> str:
    return "pending_payment" if int(fee_ngn or 0) > 0 else "confirmed"


def availability_label_from_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "free"
    if any(status == "in_progress" for status in statuses):
        return "in_session"
    if any(status == "confirmed" for status in statuses):
        return "booked"
    if any(status == "pending_payment" for status in statuses):
        return "awaiting_payment"
    return "booked"


def format_fee(amount_ngn: int, currency: str = "NGN") -> str:
    ngn = max(0, int(amount_ngn or 0))
    if currency.upper() == "NGN":
        return f"₦{ngn:,}"
    return f"{currency.upper()} {ngn:,}"


def _get_or_create_user_id(s: Session, phone_e164: str) -> Optional[str]:
    return s.execute(select(User.id).where(User.phone_e164 == phone_e164)).scalar_one_or_none()


def _get_flow(s: Session, phone_e164: str) -> TelehealthPatientFlow:
    row = s.execute(
        select(TelehealthPatientFlow).where(TelehealthPatientFlow.phone_e164 == phone_e164)
    ).scalar_one_or_none()
    if row:
        return row
    row = TelehealthPatientFlow(
        id=new_id("thf"),
        phone_e164=phone_e164,
        user_id=_get_or_create_user_id(s, phone_e164),
        step="idle",
        profession_code=None,
        selected_provider_id=None,
        pending_session_id=None,
        updated_at=now_iso(),
    )
    s.add(row)
    s.flush()
    return row


def _reset_flow(flow: TelehealthPatientFlow) -> None:
    flow.step = "idle"
    flow.profession_code = None
    flow.selected_provider_id = None
    flow.pending_session_id = None
    flow.updated_at = now_iso()


def cancel_telehealth_flow(phone_e164: str) -> None:
    phone = normalize_phone_e164(phone_e164)
    with session_scope() as s:
        flow = s.execute(
            select(TelehealthPatientFlow).where(TelehealthPatientFlow.phone_e164 == phone)
        ).scalar_one_or_none()
        if flow:
            _reset_flow(flow)


def _active_providers_stmt(profession: Optional[str] = None):
    clauses = [TelehealthProvider.status == "active"]
    if profession:
        clauses.append(TelehealthProvider.profession == profession)
    return select(TelehealthProvider).where(and_(*clauses)).order_by(TelehealthProvider.full_name.asc())


def _busy_provider_ids(s: Session, profession: Optional[str] = None) -> set[str]:
    stmt = (
        select(TelehealthSession.provider_id)
        .where(TelehealthSession.status.in_(_OPEN_SESSION_STATUSES))
        .distinct()
    )
    if profession:
        stmt = stmt.join(
            TelehealthProvider,
            TelehealthProvider.id == TelehealthSession.provider_id,
        ).where(TelehealthProvider.profession == profession)
    return set(s.execute(stmt).scalars().all())


def _pending_payment_count(s: Session, profession: Optional[str] = None) -> int:
    stmt = select(func.count(TelehealthSession.id)).where(TelehealthSession.status == "pending_payment")
    if profession:
        stmt = stmt.join(
            TelehealthProvider,
            TelehealthProvider.id == TelehealthSession.provider_id,
        ).where(TelehealthProvider.profession == profession)
    return int(s.execute(stmt).scalar_one() or 0)


def provider_availability_label(s: Session, provider_id: str) -> str:
    """free | awaiting_payment | booked | in_session"""
    rows = s.execute(
        select(TelehealthSession.status).where(
            TelehealthSession.provider_id == provider_id,
            TelehealthSession.status.in_(("pending_payment",) + _OPEN_SESSION_STATUSES),
        )
    ).scalars().all()
    return availability_label_from_statuses(list(rows))


def _available_providers_stmt(s: Session, profession: Optional[str] = None):
    busy = _busy_provider_ids(s, profession)
    clauses = [TelehealthProvider.status == "active"]
    if profession:
        clauses.append(TelehealthProvider.profession == profession)
    stmt = select(TelehealthProvider).where(and_(*clauses))
    if busy:
        stmt = stmt.where(TelehealthProvider.id.not_in(busy))
    return stmt.order_by(TelehealthProvider.full_name.asc())


def _send_patient_telehealth_message(*, phone: str, body: str, channel: str):
    """WhatsApp: template fallback outside 24h window. SMS: direct send."""
    from app.messaging import SendResult

    if channel == "whatsapp":
        return send_whatsapp_broadcast(to_phone_e164=phone, body=body)
    return send_message(to_phone_e164=phone, body=body, channel="sms")


def _provider_rating_summary(s: Session, provider_id: str) -> tuple[float, int]:
    avg, cnt = s.execute(
        select(func.avg(ProviderRating.score), func.count(ProviderRating.id)).where(
            ProviderRating.provider_id == provider_id
        )
    ).one()
    return (float(avg) if avg is not None else 0.0, int(cnt or 0))


def list_professions_with_counts() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(
            select(TelehealthProvider.profession, func.count(TelehealthProvider.id))
            .where(TelehealthProvider.status == "active")
            .group_by(TelehealthProvider.profession)
            .order_by(TelehealthProvider.profession.asc())
        ).all()
        result: list[dict[str, Any]] = []
        for code, total in rows:
            busy = _busy_provider_ids(s, code)
            pending = _pending_payment_count(s, code)
            available = max(0, int(total) - len(busy))
            result.append(
                {
                    "code": code,
                    "label": profession_label(code),
                    "count": available,
                    "total": int(total),
                    "busy": len(busy),
                    "pending": pending,
                }
            )
    return result


def _profession_menu_text() -> str:
    items = list_professions_with_counts()
    available = [item for item in items if item["count"] > 0]
    if not available:
        busy_total = sum(item.get("busy", 0) for item in items)
        pending_total = sum(item.get("pending", 0) for item in items)
        if busy_total:
            return (
                "All telehealth clinicians are busy right now. Please try again in a few minutes "
                "or send Helpdesk for assistance."
            )
        if pending_total:
            return (
                "Clinicians have payment-pending bookings right now. Please try again shortly "
                "or send Helpdesk for assistance."
            )
        return (
            "Telehealth booking is not open yet — no clinicians are listed as available. "
            "Please try again later or send Helpdesk for assistance."
        )
    lines = ["Telehealth — choose a specialty (reply with the number):"]
    for i, item in enumerate(available, start=1):
        suffix = f" ({item['count']} available now)"
        if item.get("busy"):
            suffix += f" — {item['busy']} busy"
        if item.get("pending"):
            suffix += f" — {item['pending']} awaiting payment"
        lines.append(f"{i}. {item['label']}{suffix}")
    lines.append("\nReply CANCEL to exit telehealth booking.")
    return "\n".join(lines)


def _provider_menu_text(s: Session, profession: str) -> str:
    providers = s.execute(_available_providers_stmt(s, profession)).scalars().all()
    if not providers:
        busy = len(_busy_provider_ids(s, profession))
        pending = _pending_payment_count(s, profession)
        if busy:
            return (
                f"All {profession_label(profession)} clinicians are busy right now ({busy} in session or booked). "
                "Please try again in a few minutes, choose another specialty (BACK), or send Helpdesk."
            )
        if pending:
            return (
                f"All {profession_label(profession)} clinicians currently have payment-pending bookings ({pending}). "
                "Please try again in a few minutes, choose another specialty (BACK), or send Helpdesk."
            )
        return (
            f"No providers listed for {profession_label(profession)}. "
            "Reply TELEHEALTH to start again or CANCEL to exit."
        )
    lines = [f"Telehealth — {profession_label(profession)} (reply with the number):"]
    for i, p in enumerate(providers, start=1):
        avg, cnt = _provider_rating_summary(s, p.id)
        rating = f"★ {avg:.1f} ({cnt})" if cnt else "No ratings yet"
        fee = format_fee(p.fee_ngn, p.currency)
        title = (p.title or "").strip()
        name = (p.full_name or "").strip()
        display = f"{title} {name}".strip() if title else name
        avail = provider_availability_label(s, p.id)
        suffix = ""
        if avail == "awaiting_payment":
            suffix = " — pending payment"
        elif avail == "free":
            suffix = " — available now"
        lines.append(f"{i}. {display} — {fee} — {rating}{suffix}")
        if (p.bio or "").strip():
            bio = p.bio.strip()
            if len(bio) > 72:
                bio = bio[:69].rstrip() + "…"
            lines.append(f"   {bio}")
    lines.append("\nReply BACK for specialties or CANCEL to exit.")
    return "\n".join(lines)


def _confirm_text(s: Session, provider: TelehealthProvider) -> str:
    fee = format_fee(provider.fee_ngn, provider.currency)
    avg, cnt = _provider_rating_summary(s, provider.id)
    rating = f"Average rating: {avg:.1f}/5 ({cnt} reviews)" if cnt else "No ratings yet"
    name = ((provider.title or "") + " " + (provider.full_name or "")).strip()
    return (
        f"Confirm telehealth booking:\n"
        f"Provider: {name} ({profession_label(provider.profession)})\n"
        f"Consultation fee: {fee}\n"
        f"{rating}\n\n"
        "Reply YES to confirm and receive call instructions.\n"
        "Reply BACK to choose another provider or CANCEL to exit."
    )


def _session_ref(session_id: str) -> str:
    return session_id.replace("ths_", "TH-")[:16].upper()


def _provider_display_name(provider: Optional[TelehealthProvider], *, override: Optional[str] = None) -> str:
    if override:
        return override.strip()
    if provider is None:
        return "Your clinician"
    return ((provider.title or "") + " " + (provider.full_name or "")).strip() or "Your clinician"


def _format_whatsapp_contact(phone_e164: str) -> tuple[str, str]:
    digits = "".join(c for c in (phone_e164 or "") if c.isdigit())
    if not digits:
        return "", ""
    display = phone_e164.strip()
    if display and not display.startswith("+"):
        display = f"+{digits}"
    return display, f"https://wa.me/{digits}"


def _call_instructions(
    session_id: str,
    provider: Optional[TelehealthProvider] = None,
    *,
    channel: str,
    billing_status: str = "pending",
    payment_url: Optional[str] = None,
    payment_account_number: Optional[str] = None,
    payment_bank_name: Optional[str] = None,
    payment_account_name: Optional[str] = None,
    call_initiated: bool = False,
    call_note: Optional[str] = None,
    fee_ngn: Optional[int] = None,
    currency: Optional[str] = None,
    provider_name: Optional[str] = None,
    provider_contact_phone: Optional[str] = None,
) -> str:
    if fee_ngn is not None:
        amount_ngn = fee_ngn
    elif provider is not None:
        amount_ngn = provider.fee_ngn
    else:
        amount_ngn = 0
    cur = currency or (provider.currency if provider is not None else "NGN")
    fee = format_fee(amount_ngn, cur)
    ref = _session_ref(session_id)
    lines: list[str] = []

    if billing_status == "pending" and amount_ngn > 0:
        lines.append(f"Telehealth booking (ref {ref}). Consultation fee: {fee} — payment required.")
        lines.append("")
        lines.append("HOW TO PAY:")
        if payment_account_number and payment_bank_name:
            lines.extend(
                [
                    "Bank transfer (Paystack dedicated account):",
                    f"Bank: {payment_bank_name}",
                    f"Account number: {payment_account_number}",
                ]
            )
            if payment_account_name:
                lines.append(f"Account name: {payment_account_name}")
            lines.extend(
                [
                    f"Amount: {fee} exactly",
                    f"Reference: {ref}",
                    "",
                    "Transfer the exact amount from your bank app. When Paystack receives it, "
                    "we will automatically send your video call instructions on this chat.",
                ]
            )
        elif payment_url:
            lines.append(f"Pay online (card, bank transfer, USSD, or QR):\n{payment_url}")
            lines.append("")
            lines.append(
                "When payment is confirmed, we will automatically send video call instructions on this chat."
            )
        else:
            lines.extend(
                [
                    "Your payment account is being generated — we will send bank details shortly.",
                    f"If nothing arrives in a few minutes, send: Helpdesk telehealth payment ref {ref}",
                ]
            )
        lines.extend(
            [
                "",
                "After your visit: RATE 1-5 optional comment",
                "Example: RATE 5 Very helpful consultation",
            ]
        )
        return "\n".join(lines)

    billing_line = {
        "paid": f"Fee: {fee} (paid — thank you).",
        "waived": f"Fee: {fee} (waived).",
        "pending": f"Fee: {fee} (payment pending).",
    }.get(billing_status, f"Fee: {fee} (billing: {billing_status}).")

    if channel == "whatsapp":
        lines = [f"Telehealth ready (ref {ref}). {billing_line}", ""]
        clinician = _provider_display_name(provider, override=provider_name)
        contact = (provider_contact_phone or ((provider.contact_phone_e164 or "") if provider else "")).strip()
        if call_initiated:
            lines.append("We are connecting your WhatsApp video call now — please accept when prompted.")
        elif contact:
            display, wa_link = _format_whatsapp_contact(contact)
            lines.extend(
                [
                    f"Clinician: {clinician}",
                    "",
                    "Start your video consultation on your clinician's WhatsApp (not this Kareconnect line):",
                    f"Phone: {display}",
                ]
            )
            if wa_link:
                lines.append(f"Open chat: {wa_link}")
            lines.extend(
                [
                    "",
                    f"Open that chat and start a video call with your clinician when ready. Mention ref {ref}.",
                ]
            )
        else:
            lines.extend(
                [
                    f"Clinician: {clinician}",
                    "We will send your clinician's WhatsApp number shortly, or a coordinator will reach out here.",
                ]
            )
        if call_note:
            lines.extend(["", f"Call note: {call_note}"])
        lines.extend(
            [
                "",
                f"Privacy: use booking ref {ref} if you contact helpdesk.",
                "",
                "After your visit: RATE 1-5 optional comment",
                "Example: RATE 5 Very helpful consultation",
            ]
        )
        return "\n".join(lines)

    contact = (provider_contact_phone or ((provider.contact_phone_e164 or "") if provider else "")).strip()
    clinician = _provider_display_name(provider, override=provider_name)
    if contact and billing_status in ("paid", "waived"):
        display, wa_link = _format_whatsapp_contact(contact)
        extra = f" Contact {clinician} on WhatsApp: {display}."
        if wa_link:
            extra += f" {wa_link}"
        return (
            f"Telehealth ready (ref {ref}). {billing_line}{extra} "
            f"Mention ref {ref} when you connect."
        )
    return (
        f"Telehealth ready (ref {ref}). {billing_line} "
        "A team member will contact you to arrange the consultation."
    )


def _notify_operator_telehealth_event(
    *,
    event: str,
    session_id: str,
    provider_id: str,
    patient_phone_e164: str,
    channel: str,
    fee_ngn: int,
    billing_status: str,
) -> None:
    try:
        integ = get_effective_integration()
        url = (integ.operator_webhook_url or "").strip()
        if not url:
            return
        base = (integ.hc_base_url or "").rstrip("/")
        payload = {
            "event": event,
            "session_id": session_id,
            "admin_url": f"{base}/admin/telehealth-session/details/{session_id}",
            "provider_id": provider_id,
            "patient_phone_e164": patient_phone_e164,
            "channel": channel,
            "fee_ngn": fee_ngn,
            "billing_status": billing_status,
        }
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        if not (200 <= r.status_code < 300):
            _log.warning("telehealth operator_webhook HTTP %s: %s", r.status_code, (r.text or "")[:500])
    except Exception as exc:
        _log.warning("telehealth operator_webhook failed: %s", exc)


def notify_provider_new_booking(session_id: str, *, payment_received: bool = False) -> None:
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return
        raw_phone = (provider.contact_phone_e164 or "").strip()
        if not raw_phone:
            return
        dest = normalize_phone_e164(normalize_at_phone_number(raw_phone))
        patient = (
            mask_phone_for_display(session.patient_phone_e164, session_id=session.id)
            if session.mask_patient_number
            else session.patient_phone_e164
        )
        ref = _session_ref(session_id)
        fee = format_fee(session.fee_ngn, session.currency)
        name = ((provider.title or "") + " " + (provider.full_name or "")).strip()
        if payment_received:
            headline = f"Payment received — telehealth {ref}"
            tail = "Patient is ready for the video consultation."
        else:
            headline = f"New telehealth booking {ref}"
            tail = (
                f"Billing: {session.billing_status}. "
                "You will be notified again when payment is confirmed."
                if session.billing_status == "pending" and session.fee_ngn > 0
                else "Patient can proceed to the consultation."
            )
        body = (
            f"{headline}\n"
            f"Provider: {name}\n"
            f"Specialty: {profession_label(session.profession_snapshot)}\n"
            f"Patient: {patient}\n"
            f"Fee: {fee}\n"
            f"Patient channel: {session.channel.upper()}\n"
            f"{tail}"
        )
    res = send_whatsapp_broadcast(to_phone_e164=dest, body=body)
    if not res.ok:
        res = send_message(to_phone_e164=dest, body=body, channel="sms")
    if not res.ok:
        _log.warning("provider telehealth notify failed: %s", res.error)


def _helpdesk_outbound_channel(patient_channel: str) -> Literal["sms", "whatsapp"]:
    if (patient_channel or "").lower().strip() == "whatsapp":
        return "whatsapp"
    return "sms"


def notify_helpdesk_telehealth_booking(
    session_id: str,
    *,
    event: Literal["booked", "paid"],
    extra_note: Optional[str] = None,
) -> tuple[bool, str]:
    """Alert helpdesk staff (HC_HELPDESK_PHONE / admin field) about a telehealth booking."""
    raw = (get_effective_integration().helpdesk_phone_e164 or "").strip()
    if not raw:
        return False, "Helpdesk phone not configured (helpdesk_phone_e164 / HC_HELPDESK_PHONE)."

    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return False, "Provider not found"
        ref = _session_ref(session_id)
        fee = format_fee(session.fee_ngn, session.currency)
        patient = (
            mask_phone_for_display(session.patient_phone_e164, session_id=session.id)
            if session.mask_patient_number
            else session.patient_phone_e164
        )
        prov_name = ((provider.title or "") + " " + (provider.full_name or "")).strip()
        specialty = profession_label(session.profession_snapshot)
        billing = session.billing_status
        channel = session.channel
        fee_ngn = session.fee_ngn
        patient_phone = session.patient_phone_e164
        base = (get_effective_integration().hc_base_url or "").rstrip("/")
        admin_url = f"{base}/admin/telehealth-session/details/{session_id}"

    dest = normalize_phone_e164(normalize_at_phone_number(raw))
    if dest == normalize_phone_e164(patient_phone):
        return False, "Helpdesk phone matches patient — alert skipped."

    if event == "paid":
        headline = f"Telehealth PAID {ref}"
        tail = "Patient call instructions have been sent."
    else:
        headline = f"New telehealth booking {ref}"
        if billing == "pending" and fee_ngn > 0:
            tail = "Awaiting patient payment."
        else:
            tail = "Patient can proceed to consultation."

    lines = [
        f"Kareconnect {headline}",
        f"Provider: {prov_name} ({specialty})",
        f"Patient: {patient}",
        f"Fee: {fee} ({billing})",
        f"Channel: {channel.upper()}",
        tail,
        f"Admin: {admin_url}",
    ]
    if extra_note:
        lines.insert(-1, extra_note)
    body = "\n".join(lines)
    out_ch = _helpdesk_outbound_channel(channel)
    res = send_message(to_phone_e164=dest, body=body, channel=out_ch)
    if not res.ok:
        _log.warning("helpdesk telehealth notify failed: %s", res.error)
        return False, res.error or "send failed"
    return True, f"Sent on {out_ch.upper()}"


def resend_patient_telehealth_message(session_id: str, *, notify_provider: bool = True) -> tuple[bool, str]:
    """Resend payment details or post-payment call instructions to the patient."""
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return False, "Provider not found"
        billing = session.billing_status
        channel = session.channel
        phone = session.patient_phone_e164
        payment_url = session.payment_url
        payment_account_number = session.payment_account_number
        payment_bank_name = session.payment_bank_name
        payment_account_name = session.payment_account_name
        fee_ngn = provider.fee_ngn
        currency = provider.currency
        prov_name = _provider_display_name(provider)
        prov_contact = (provider.contact_phone_e164 or "").strip()

    if billing in ("paid", "waived"):
        ok, msg = deliver_post_payment_patient_message(session_id, notify_provider=notify_provider, force=True)
        return ok, msg

    body = _call_instructions(
        session_id,
        provider=None,
        channel=channel,
        billing_status=billing,
        payment_url=payment_url,
        payment_account_number=payment_account_number,
        payment_bank_name=payment_bank_name,
        payment_account_name=payment_account_name,
        fee_ngn=fee_ngn,
        currency=currency,
        provider_name=prov_name,
        provider_contact_phone=prov_contact,
    )
    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    res = _send_patient_telehealth_message(phone=phone, body=body, channel=ch)
    if not res.ok:
        return False, res.error or "send failed"
    return True, "Booking/payment details sent to patient"


def resend_provider_telehealth_notification(session_id: str) -> tuple[bool, str]:
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return False, "Provider not found"
        if not (provider.contact_phone_e164 or "").strip():
            return False, "Provider contact phone not set — edit provider profile"
        paid = session.billing_status in ("paid", "waived")

    notify_provider_new_booking(session_id, payment_received=paid)
    label = "payment confirmed" if paid else "booking"
    return True, f"Provider alert sent ({label})"


def resend_helpdesk_telehealth_notification(session_id: str) -> tuple[bool, str]:
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        event: Literal["booked", "paid"] = "paid" if session.billing_status in ("paid", "waived") else "booked"
    return notify_helpdesk_telehealth_booking(session_id, event=event)


def resend_telehealth_confirmations(session_id: str) -> list[str]:
    """Resend patient, provider, and helpdesk notifications for one session."""
    lines: list[str] = []
    ok, msg = resend_patient_telehealth_message(session_id, notify_provider=False)
    lines.append(f"Patient: {msg}" if ok else f"Patient FAILED: {msg}")
    ok, msg = resend_provider_telehealth_notification(session_id)
    lines.append(f"Provider: {msg}" if ok else f"Provider FAILED: {msg}")
    ok, msg = resend_helpdesk_telehealth_notification(session_id)
    lines.append(f"Helpdesk: {msg}" if ok else f"Helpdesk FAILED: {msg}")
    return lines


def reassign_telehealth_provider(session_id: str, new_provider_id: str) -> tuple[bool, str]:
    """Move a session to another active provider and notify patient, provider, and helpdesk."""
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        if session.status in ("completed", "cancelled"):
            return False, f"Cannot reassign {session.status} session"
        new_provider = s.get(TelehealthProvider, new_provider_id)
        if not new_provider or new_provider.status != "active":
            return False, "Choose an active provider"
        if session.provider_id == new_provider_id:
            return False, "Session is already assigned to that provider"

        session.provider_id = new_provider_id
        session.profession_snapshot = new_provider.profession
        session.fee_ngn = new_provider.fee_ngn
        session.currency = new_provider.currency
        session.updated_at = now_iso()
        billing = session.billing_status
        provider_name = ((new_provider.title or "") + " " + (new_provider.full_name or "")).strip()

    resend_patient_telehealth_message(session_id, notify_provider=False)
    notify_provider_new_booking(session_id, payment_received=billing in ("paid", "waived"))
    notify_helpdesk_telehealth_booking(
        session_id,
        event="paid" if billing in ("paid", "waived") else "booked",
        extra_note=f"Reassigned to {provider_name}.",
    )
    return True, f"Reassigned to {provider_name}. Notifications sent."


def attempt_whatsapp_call_for_session(session_id: str) -> tuple[bool, Optional[str]]:
    from app.meta_whatsapp_calling import initiate_business_call, is_calling_enabled

    if not is_calling_enabled():
        return False, None
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session or session.channel != "whatsapp" or session.call_mode != "whatsapp_video":
            return False, None
        phone = session.patient_phone_e164
    ok, call_id, err = initiate_business_call(to_phone_e164=phone, session_id=session_id)
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return ok, err
        if ok and call_id:
            session.call_id = call_id
            session.call_status = "initiated"
            session.status = "in_progress"
            session.call_note = None
        else:
            session.call_note = (err or "Call not initiated")[:500]
        session.updated_at = now
    return ok, err


def deliver_payment_account_message(session_id: str) -> None:
    """Send bank-transfer details when DVA is assigned asynchronously."""
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session or session.billing_status != "pending":
            return
        if not session.payment_account_number:
            return
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return
        body = _call_instructions(
            session_id,
            provider=None,
            channel=session.channel,
            billing_status="pending",
            payment_account_number=session.payment_account_number,
            payment_bank_name=session.payment_bank_name,
            payment_account_name=session.payment_account_name,
            payment_url=session.payment_url,
            fee_ngn=provider.fee_ngn,
            currency=provider.currency,
            provider_name=_provider_display_name(provider),
            provider_contact_phone=(provider.contact_phone_e164 or "").strip(),
        )
        phone = session.patient_phone_e164
        ch: Literal["sms", "whatsapp"] = "whatsapp" if session.channel == "whatsapp" else "sms"
    _send_patient_telehealth_message(phone=phone, body=body, channel=ch)


def deliver_post_payment_patient_message(session_id: str, *, notify_provider: bool = True, force: bool = False) -> tuple[bool, str]:
    """Send call instructions (and optional Meta call) after payment is confirmed."""
    from app.meta_whatsapp_calling import is_calling_enabled

    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        if session.payment_instructions_sent_at and not force:
            return True, "Instructions already sent"
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return False, "Provider not found"
        channel = session.channel
        phone = session.patient_phone_e164
        billing = session.billing_status
        fee_ngn = provider.fee_ngn
        currency = provider.currency
        prov_name = _provider_display_name(provider)
        prov_contact = (provider.contact_phone_e164 or "").strip()

    call_ok = False
    call_note: Optional[str] = None
    if channel == "whatsapp" and billing in ("paid", "waived"):
        try:
            call_ok, call_note = attempt_whatsapp_call_for_session(session_id)
            if not is_calling_enabled():
                call_note = None
        except Exception:
            _log.exception("whatsapp call attempt failed session=%s", session_id)
            call_ok, call_note = False, None

    body = _call_instructions(
        session_id,
        provider=None,
        channel=channel,
        billing_status=billing,
        call_initiated=call_ok,
        call_note=call_note,
        fee_ngn=fee_ngn,
        currency=currency,
        provider_name=prov_name,
        provider_contact_phone=prov_contact,
    )
    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    res = _send_patient_telehealth_message(phone=phone, body=body, channel=ch)
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if session:
            if res.ok:
                session.payment_instructions_sent_at = now
                session.billing_notes = None
            else:
                session.billing_notes = f"Patient notify failed: {res.error or 'error'}"[:2000]
            session.updated_at = now
    if not res.ok:
        _log.warning("post-payment patient notify failed session=%s: %s", session_id, res.error)
        return False, res.error or "send failed"
    if notify_provider:
        notify_provider_new_booking(session_id, payment_received=True)
    return True, f"Sent via {res.via or ch}"


def on_payment_confirmed(session_id: str, *, reference: Optional[str] = None) -> bool:
    """Mark session paid and trigger provider + patient notifications."""
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False
        already_paid = session.billing_status == "paid"
        needs_instructions = not session.payment_instructions_sent_at
        if not already_paid:
            session.billing_status = "paid"
            if session.status == "pending_payment":
                session.status = "confirmed"
            if reference:
                session.billing_reference = reference
            session.payment_url = None
            session.updated_at = now
        snap = {
            "provider_id": session.provider_id,
            "patient_phone_e164": session.patient_phone_e164,
            "channel": session.channel,
            "fee_ngn": session.fee_ngn,
        }

    if already_paid and not needs_instructions:
        return True

    if not already_paid:
        _notify_operator_telehealth_event(
            event="telehealth.session.paid",
            session_id=session_id,
            billing_status="paid",
            **snap,
        )
        notify_helpdesk_telehealth_booking(session_id, event="paid")

    ok, _detail = deliver_post_payment_patient_message(session_id)
    return ok


def finalize_new_booking(session_id: str, *, db: Optional[Session] = None) -> str:
    """Paystack link, waived flow, or immediate instructions after session row is created."""
    if db is not None:
        return _finalize_new_booking_impl(db, session_id)
    with session_scope() as s:
        return _finalize_new_booking_impl(s, session_id)


def _finalize_new_booking_impl(s: Session, session_id: str) -> str:
    from app.paystack_billing import create_telehealth_payment, is_configured as paystack_ready

    session = s.get(TelehealthSession, session_id)
    if not session:
        return "Booking failed. Send TELEHEALTH to try again."
    provider = s.get(TelehealthProvider, session.provider_id)
    if not provider:
        return "Booking failed. Send TELEHEALTH to try again."
    fee = session.fee_ngn
    channel = session.channel
    phone = session.patient_phone_e164
    provider_id = provider.id
    provider_name = provider.full_name
    provider_title = provider.title
    provider_profession = provider.profession
    provider_fee = provider.fee_ngn
    provider_currency = provider.currency
    provider_contact = (provider.contact_phone_e164 or "").strip()
    patient_name: Optional[str] = None
    if session.user_id:
        user = s.get(User, session.user_id)
        if user:
            patient_name = user.full_name

    notify_provider_new_booking(session_id, payment_received=False)
    _notify_operator_telehealth_event(
        event="telehealth.session.booked",
        session_id=session_id,
        provider_id=provider_id,
        patient_phone_e164=phone,
        channel=channel,
        fee_ngn=fee,
        billing_status="pending",
    )
    notify_helpdesk_telehealth_booking(session_id, event="booked")

    if fee <= 0:
        session.status = "confirmed"
        session.billing_status = "waived"
        session.updated_at = now_iso()
        call_ok, call_note = (False, None)
        if channel == "whatsapp":
            call_ok, call_note = attempt_whatsapp_call_for_session(session_id)
        waived_provider = TelehealthProvider(
            id=provider_id,
            full_name=provider_name,
            title=provider_title,
            profession=provider_profession,
            fee_ngn=provider_fee,
            currency=provider_currency,
            status="active",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        return _call_instructions(
            session_id,
            waived_provider,
            channel=channel,
            billing_status="waived",
            call_initiated=call_ok,
            call_note=call_note,
            provider_name=f"{provider_title or ''} {provider_name or ''}".strip(),
            provider_contact_phone=provider_contact,
        )

    if paystack_ready():
        setup = create_telehealth_payment(
            session_id=session_id,
            phone_e164=phone,
            amount_ngn=fee,
            patient_name=patient_name,
        )
        if not setup.error or setup.account_number or setup.checkout_url:
            session.payment_url = setup.checkout_url
            session.billing_reference = setup.reference
            session.paystack_customer_email = setup.customer_email
            session.payment_account_number = setup.account_number
            session.payment_bank_name = setup.bank_name
            session.payment_account_name = setup.account_name
            session.updated_at = now_iso()
            pending_provider = TelehealthProvider(
                id=provider_id,
                full_name=provider_name,
                title=provider_title,
                profession=provider_profession,
                fee_ngn=provider_fee,
                currency=provider_currency,
                status="active",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            return _call_instructions(
                session_id,
                pending_provider,
                channel=channel,
                billing_status="pending",
                payment_url=setup.checkout_url,
                payment_account_number=setup.account_number,
                payment_bank_name=setup.bank_name,
                payment_account_name=setup.account_name,
            )
        _log.warning("Paystack setup failed for %s: %s", session_id, setup.error)
        if setup.error:
            session.billing_notes = f"Paystack setup failed: {setup.error}"[:2000]
            session.updated_at = now_iso()

    fallback_provider = TelehealthProvider(
        id=provider_id,
        full_name=provider_name,
        title=provider_title,
        profession=provider_profession,
        fee_ngn=provider_fee,
        currency=provider_currency,
        status="active",
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    return _call_instructions(
        session_id,
        fallback_provider,
        channel=channel,
        billing_status="pending",
        payment_url=session.payment_url,
        payment_account_number=session.payment_account_number,
        payment_bank_name=session.payment_bank_name,
        payment_account_name=session.payment_account_name,
    )


def confirm_payment_from_browser_callback(
    session_id: Optional[str],
    *,
    reference: Optional[str] = None,
    trxref: Optional[str] = None,
) -> tuple[str, str, bool]:
    """
    Paystack redirects the patient here after checkout. Verify with Paystack API and
    confirm payment (fallback when webhook is missing or delayed).
    Returns (title, body_paragraph, instructions_sent).
    """
    from app.paystack_billing import payment_reference_for_session, verify_transaction

    sid = (session_id or "").strip()
    ref = (reference or trxref or "").strip()
    if not sid and not ref:
        return (
            "Payment",
            "Missing session reference. Return to WhatsApp and send TELEHEALTH if you need help.",
            False,
        )

    with session_scope() as s:
        session: Optional[TelehealthSession] = None
        if sid:
            session = s.get(TelehealthSession, sid)
        if not session and ref:
            session = s.execute(
                select(TelehealthSession).where(TelehealthSession.billing_reference == ref).limit(1)
            ).scalar_one_or_none()
        if not session and ref.startswith("ths_"):
            session = s.get(TelehealthSession, ref)
        if not session:
            return (
                "Payment",
                "We could not find this booking. Return to WhatsApp and send TELEHEALTH if you need help.",
                False,
            )

        sid = session.id
        display_ref = _session_ref(sid)
        billing_status = session.billing_status
        instructions_sent = bool(session.payment_instructions_sent_at)
        if not ref:
            ref = (session.billing_reference or "").strip() or payment_reference_for_session(sid)

    if billing_status == "paid":
        if not instructions_sent:
            try:
                ok, detail = deliver_post_payment_patient_message(sid)
                if ok:
                    return (
                        "Payment confirmed",
                        f"Ref {display_ref}. Video call instructions have been sent to your Kareconnect WhatsApp chat.",
                        True,
                    )
                return (
                    "Payment confirmed",
                    f"Ref {display_ref}. Payment recorded but message failed: {detail}. "
                    "Use Admin resend or return to WhatsApp shortly.",
                    False,
                )
            except Exception:
                _log.exception("paystack callback resend failed session=%s", sid)
        return (
            "Payment confirmed",
            f"Ref {display_ref}. Your payment is already recorded. Check your Kareconnect WhatsApp chat for video call instructions.",
            False,
        )

    paid, detail, _data = verify_transaction(ref)
    if not paid:
        _log.warning("paystack browser callback verify failed session=%s ref=%s: %s", sid, ref, detail)
        return (
            "Payment processing",
            f"Ref {display_ref}. {detail} If you completed payment, wait a minute and refresh this page, "
            "or return to WhatsApp — instructions are sent automatically once Paystack confirms payment.",
            False,
        )

    try:
        on_payment_confirmed(sid, reference=ref)
    except Exception:
        _log.exception("paystack browser callback confirm failed session=%s", sid)
        return (
            "Payment received",
            f"Ref {display_ref}. Payment verified with Paystack but we could not send WhatsApp instructions automatically. "
            "Return to your Kareconnect chat or contact support with this reference.",
            False,
        )
    return (
        "Payment received",
        f"Thank you. Ref {display_ref}. Video call instructions have been sent to your Kareconnect WhatsApp chat.",
        True,
    )


def apply_paystack_webhook_event(event: dict[str, Any]) -> None:
    event_name = (event.get("event") or "").strip()

    if event_name == "dedicatedaccount.assign.success":
        from app.paystack_billing import apply_dedicated_account_assigned

        session_id = apply_dedicated_account_assigned(event)
        if session_id:
            deliver_payment_account_message(session_id)
        return

    if event_name != "charge.success":
        return
    session_id = None
    try:
        from app.paystack_billing import extract_session_id_from_event

        session_id = extract_session_id_from_event(event)
    except Exception:
        _log.exception("paystack session_id extract failed")
    if not session_id:
        _log.warning("paystack charge.success: could not resolve telehealth session")
        return
    data = event.get("data") or {}
    reference = data.get("reference")
    on_payment_confirmed(session_id, reference=str(reference) if reference else None)


def apply_meta_call_webhook_event(event: dict[str, Any]) -> None:
    session_id = (event.get("session_id") or "").strip()
    call_id = event.get("call_id")
    call_event = (event.get("event") or "").strip().lower()
    now = now_iso()
    ended_session_id: Optional[str] = None
    with session_scope() as s:
        session: Optional[TelehealthSession] = None
        if session_id:
            session = s.get(TelehealthSession, session_id)
        if not session and call_id:
            session = (
                s.execute(select(TelehealthSession).where(TelehealthSession.call_id == str(call_id)))
                .scalars()
                .first()
            )
        if not session:
            return
        if call_id and not session.call_id:
            session.call_id = str(call_id)
        if call_event in ("connect", "connected", "accepted"):
            session.call_status = "connected"
            session.status = "in_progress"
            session.provider_joined = True
            if not session.call_started_at:
                session.call_started_at = now
        elif call_event in ("terminate", "terminated", "ended"):
            session.call_status = "ended"
            if not session.call_ended_at:
                session.call_ended_at = now
            if not session.provider_joined:
                session.status = "no_show"
                session.call_note = (session.call_note or "Call ended without provider connection")[:500]
            elif session.status != "completed":
                session.status = "in_progress"
            ended_session_id = session.id
        elif call_event in ("ringing", "initiated"):
            session.call_status = call_event
            session.status = "in_progress"
        elif call_event in ("failed", "missed", "rejected", "declined", "busy"):
            session.call_status = call_event[:32]
            if not session.provider_joined:
                session.status = "no_show"
                session.call_note = (session.call_note or f"Call {call_event}")[:500]
            if not session.call_ended_at:
                session.call_ended_at = now
            ended_session_id = session.id
        elif call_event:
            session.call_status = call_event[:32]
        session.updated_at = now

    if ended_session_id:
        send_post_call_feedback_prompts(ended_session_id)


def send_post_call_feedback_prompts(session_id: str, *, force: bool = False) -> tuple[bool, str]:
    """After a call ends, ask the patient to RATE and update the provider/helpdesk."""
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        if session.feedback_prompt_sent_at and not force:
            return True, "Feedback prompts already sent"
        provider = s.get(TelehealthProvider, session.provider_id)
        if not provider:
            return False, "Provider not found"
        ref = _session_ref(session_id)
        channel = session.channel
        patient_phone = session.patient_phone_e164
        provider_joined = session.provider_joined
        call_started = session.call_started_at
        call_ended = session.call_ended_at
        billing = session.billing_status
        mask_patient = session.mask_patient_number
        has_rating = (
            s.execute(select(ProviderRating.id).where(ProviderRating.session_id == session_id).limit(1))
            .scalar_one_or_none()
            is not None
        )
        prov_name = ((provider.title or "") + " " + (provider.full_name or "")).strip()
        prov_phone = (provider.contact_phone_e164 or "").strip()
        base = (get_effective_integration().hc_base_url or "").rstrip("/")
        admin_url = f"{base}/admin/telehealth-session/details/{session_id}"

    patient_lines = [
        f"Your telehealth consultation (ref {ref}) has ended.",
        "",
    ]
    if provider_joined:
        patient_lines.append("Thank you for joining the call.")
    else:
        patient_lines.append("We could not confirm a completed video call for this session.")
    if not has_rating:
        patient_lines.extend(
            [
                "",
                "Please rate your visit:",
                "RATE 1-5 optional comment",
                "Example: RATE 5 Very helpful consultation",
            ]
        )
    patient_body = "\n".join(patient_lines)

    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    patient_res = send_message(to_phone_e164=patient_phone, body=patient_body, channel=ch)

    provider_msg = "not sent (no contact phone)"
    if prov_phone:
        joined = "Yes" if provider_joined else "No"
        duration = ""
        if call_started and call_ended:
            duration = f"\nCall window: {call_started[:16]} → {call_ended[:16]} UTC"
        patient_display = (
            mask_phone_for_display(patient_phone, session_id=session_id) if mask_patient else patient_phone
        )
        prov_body = (
            f"Telehealth call ended — ref {ref}\n"
            f"Provider: {prov_name}\n"
            f"Patient: {patient_display}\n"
            f"Provider joined call: {joined}\n"
            f"Billing: {billing}{duration}\n"
            f"Patient feedback: {'received' if has_rating else 'pending (RATE in WhatsApp)'}\n"
            f"Admin session: {admin_url}"
        )
        dest = normalize_phone_e164(normalize_at_phone_number(prov_phone))
        prov_res = send_whatsapp_broadcast(to_phone_e164=dest, body=prov_body)
        if not prov_res.ok:
            prov_res = send_message(to_phone_e164=dest, body=prov_body, channel="sms")
        provider_msg = "sent" if prov_res.ok else (prov_res.error or "failed")

    notify_helpdesk_telehealth_booking(
        session_id,
        event="paid" if billing in ("paid", "waived") else "booked",
        extra_note=(
            f"Call ended. Provider joined: {'yes' if provider_joined else 'no'}. "
            f"Patient rating: {'yes' if has_rating else 'pending'}."
        ),
    )

    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if session:
            session.feedback_prompt_sent_at = now
            session.updated_at = now

    if not patient_res.ok:
        return False, f"Patient prompt failed: {patient_res.error or 'error'}"
    return True, f"Patient prompt sent; provider alert {provider_msg}"


def mark_session_call_started(session_id: str, *, provider_joined: bool = True) -> tuple[bool, str]:
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        if session.status in ("completed", "cancelled"):
            return False, f"Session is {session.status}"
        session.status = "in_progress"
        session.call_status = "connected" if provider_joined else "initiated"
        session.provider_joined = provider_joined
        if not session.call_started_at:
            session.call_started_at = now
        session.updated_at = now
    return True, "Session marked in progress"


def mark_session_call_ended(
    session_id: str,
    *,
    provider_joined: bool,
    send_prompts: bool = True,
) -> tuple[bool, str]:
    now = now_iso()
    with session_scope() as s:
        session = s.get(TelehealthSession, session_id)
        if not session:
            return False, "Session not found"
        session.call_status = "ended"
        session.provider_joined = provider_joined
        if not session.call_ended_at:
            session.call_ended_at = now
        if provider_joined:
            if session.status != "completed":
                session.status = "in_progress"
        else:
            session.status = "no_show"
            session.call_note = (session.call_note or "Marked no-show by admin")[:500]
        session.updated_at = now

    if send_prompts:
        ok, detail = send_post_call_feedback_prompts(session_id, force=True)
        return ok, f"Call ended ({'joined' if provider_joined else 'no-show'}). {detail}"
    label = "joined" if provider_joined else "no-show"
    return True, f"Call marked ended ({label})"


def is_in_telehealth_flow(phone_e164: str) -> bool:
    with session_scope() as s:
        row = s.execute(
            select(TelehealthPatientFlow.step).where(TelehealthPatientFlow.phone_e164 == phone_e164)
        ).scalar_one_or_none()
    return bool(row and row != "idle")


def handle_telehealth_message(
    *,
    phone_e164: str,
    channel: Literal["sms", "whatsapp"],
    body: str,
) -> Optional[str]:
    """
    Returns reply text if this message is handled by telehealth flow, else None.
    """
    text = (body or "").strip()
    upper = text.upper()
    first = re.sub(r"[^A-Z0-9]", "", upper.split()[0]) if upper.split() else ""

    with session_scope() as s:
        flow = _get_flow(s, phone_e164)
        if flow.step == "idle":
            return None

        if first in ("CANCEL", "EXIT"):
            _reset_flow(flow)
            return "Telehealth booking cancelled."

        if upper in ("BACK", "0") and flow.step == "provider":
            flow.step = "profession"
            flow.profession_code = None
            flow.selected_provider_id = None
            flow.updated_at = now_iso()
            return _profession_menu_text()

        if upper in ("BACK", "0") and flow.step == "confirm":
            flow.step = "provider"
            flow.selected_provider_id = None
            flow.updated_at = now_iso()
            if flow.profession_code:
                return _provider_menu_text(s, flow.profession_code)
            flow.step = "profession"
            return _profession_menu_text()

        if flow.step == "profession":
            if not text.isdigit():
                return None
            items = [item for item in list_professions_with_counts() if item["count"] > 0]
            idx = int(text)
            if idx < 1 or idx > len(items):
                return f"Reply 1–{len(items)} to choose a specialty, or CANCEL to exit."
            code = items[idx - 1]["code"]
            flow.step = "provider"
            flow.profession_code = code
            flow.updated_at = now_iso()
            return _provider_menu_text(s, code)

        if flow.step == "provider":
            if not text.isdigit():
                return None
            if not flow.profession_code:
                flow.step = "profession"
                return _profession_menu_text()
            providers = list(s.execute(_available_providers_stmt(s, flow.profession_code)).scalars().all())
            idx = int(text)
            if idx < 1 or idx > len(providers):
                busy = len(_busy_provider_ids(s, flow.profession_code))
                if busy:
                    return (
                        f"Reply 1–{len(providers)} to choose an available provider ({busy} busy — not listed), "
                        "BACK, or CANCEL."
                    )
                return f"Reply 1–{len(providers)} to choose a provider, BACK, or CANCEL."
            provider = providers[idx - 1]
            flow.step = "confirm"
            flow.selected_provider_id = provider.id
            flow.updated_at = now_iso()
            return _confirm_text(s, provider)

        if flow.step == "confirm":
            if upper not in ("YES", "Y", "CONFIRM", "OK"):
                return None
            if not flow.selected_provider_id:
                _reset_flow(flow)
                return "Session expired. Send TELEHEALTH to start again."
            provider = s.get(TelehealthProvider, flow.selected_provider_id)
            if not provider or provider.status != "active":
                _reset_flow(flow)
                return "That provider is no longer available. Send TELEHEALTH to start again."
            if provider_availability_label(s, provider.id) != "free":
                flow.step = "provider"
                flow.selected_provider_id = None
                flow.updated_at = now_iso()
                return (
                    f"{_provider_display_name(provider)} is no longer free (booked or in session). "
                    f"{_provider_menu_text(s, flow.profession_code or provider.profession)}"
                )
            user_id = _get_or_create_user_id(s, phone_e164)
            now = now_iso()
            session_id = new_id("ths")
            s.add(
                TelehealthSession(
                    id=session_id,
                    user_id=user_id,
                    patient_phone_e164=phone_e164,
                    provider_id=provider.id,
                    channel=channel,
                    profession_snapshot=provider.profession,
                    status=new_booking_session_status(provider.fee_ngn),
                    fee_ngn=provider.fee_ngn,
                    currency=provider.currency,
                    billing_status="pending",
                    call_mode="whatsapp_video" if channel == "whatsapp" else "phone",
                    mask_patient_number=provider.mask_patient_number,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            s.flush()
            _reset_flow(flow)
            return finalize_new_booking(session_id, db=s)

    return None


def start_telehealth_flow(*, phone_e164: str) -> str:
    from app.appointments import cancel_appointment_flow

    cancel_appointment_flow(phone_e164)
    with session_scope() as s:
        flow = _get_flow(s, phone_e164)
        flow.step = "profession"
        flow.profession_code = None
        flow.selected_provider_id = None
        flow.pending_session_id = None
        flow.updated_at = now_iso()
    return _profession_menu_text()


def parse_rate_command(body: str, *, keyword: str = "RATE") -> tuple[Optional[int], Optional[str]]:
    text = (body or "").strip()
    if not text:
        return None, None
    parts = text.split(None, 1)
    if parts[0].upper() != keyword.upper().strip():
        return None, None
    rest = parts[1] if len(parts) > 1 else ""
    if not rest:
        return None, None
    rest_parts = rest.split(None, 1)
    score_token = rest_parts[0].strip()
    if not score_token.isdigit():
        return None, None
    score = int(score_token)
    comment = rest_parts[1].strip() if len(rest_parts) > 1 else None
    return score, comment


def submit_rating(
    *,
    phone_e164: str,
    score: int,
    comment: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    if score < 1 or score > 5:
        return "Rating must be 1–5. Example: RATE 5 Great consultation"
    with session_scope() as s:
        session: Optional[TelehealthSession] = None
        if session_id:
            session = s.get(TelehealthSession, session_id)
        if not session:
            session = (
                s.execute(
                    select(TelehealthSession)
                    .where(
                        TelehealthSession.patient_phone_e164 == phone_e164,
                        TelehealthSession.status.in_(["pending_payment", "confirmed", "completed", "in_progress"]),
                    )
                    .order_by(TelehealthSession.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if not session:
            return "No recent telehealth session found to rate. Book with TELEHEALTH first."
        existing = s.execute(
            select(ProviderRating.id).where(ProviderRating.session_id == session.id)
        ).scalar_one_or_none()
        if existing:
            return "You already rated this session. Thank you."
        user_id = _get_or_create_user_id(s, phone_e164)
        s.add(
            ProviderRating(
                id=new_id("thr"),
                session_id=session.id,
                provider_id=session.provider_id,
                user_id=user_id,
                patient_phone_e164=phone_e164,
                score=score,
                comment=(comment or "").strip() or None,
                created_at=now_iso(),
            )
        )
        session.status = "completed"
        session.completed_at = now_iso()
        session.updated_at = now_iso()
        _recompute_provider_rating(s, session.provider_id)
    return f"Thank you — rated {score}/5. Your feedback helps other patients."


def _recompute_provider_rating(s: Session, provider_id: str) -> None:
    provider = s.get(TelehealthProvider, provider_id)
    if not provider:
        return
    avg, cnt = _provider_rating_summary(s, provider_id)
    provider.avg_rating = round(avg, 2)
    provider.rating_count = cnt
    provider.updated_at = now_iso()


def mask_phone_for_display(phone_e164: str, *, session_id: Optional[str] = None) -> str:
    digits = "".join(c for c in (phone_e164 or "") if c.isdigit())
    if session_id:
        tail = session_id.replace("ths_", "")[-4:].upper()
        return f"Patient ···{digits[-4:] if len(digits) >= 4 else '????'} ({tail})"
    if len(digits) >= 4:
        return f"···{digits[-4:]}"
    return "···"
