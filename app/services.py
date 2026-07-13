from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

import requests
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session

from app.africastalking import normalize_at_phone_number
from app.karevault_client import fetch_symptom_checker_reply
from app.database import session_scope
from app.integration_config import get_effective_integration
from app.messaging import Channel as MessagingChannel
from app.messaging import send_message
from app.models import (
    AppSettings,
    Appointment,
    Enrollment,
    Escalation,
    HelpdeskTicket,
    InboundMessage,
    MessageOutbox,
    RpmEnrollment,
    User,
)
from app.default_help_menu import DEFAULT_HELP_MENU_TEXT
from app.util import best_faq_answer, load_faqs, new_id, normalize_phone_e164, now_iso, safe_json_dumps


Language = Literal["en", "pcm", "ha", "ig", "yo"]
Program = Literal["maternal_child", "chronic", "youth", "kidney"]
Channel = Literal["sms", "whatsapp", "ussd", "web"]


def _split_keywords(value: str) -> list[str]:
    return [x.strip().upper() for x in value.split(",") if x.strip()]


def ensure_app_settings() -> None:
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if row:
            return
        s.add(AppSettings(id=1))


def get_app_settings() -> AppSettings:
    ensure_app_settings()
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            raise RuntimeError("app settings missing")
        s.expunge(row)
        return row


def get_help_menu_text() -> str:
    """Patient-facing help menu from admin settings (safe outside a DB session)."""
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            return DEFAULT_HELP_MENU_TEXT
        text = (row.default_help_menu_text or "").strip()
        return text or DEFAULT_HELP_MENU_TEXT


def get_keywords_for_channel(channel: str, kind: str) -> list[str]:
    cfg = get_app_settings()
    if kind == "join":
        raw = cfg.whatsapp_join_keywords if channel == "whatsapp" else cfg.sms_join_keywords
        return _split_keywords(raw)
    if kind == "help":
        raw = cfg.whatsapp_help_keywords if channel == "whatsapp" else cfg.sms_help_keywords
        kws = _split_keywords(raw)
        return kws or ["INFO", "MENU", "COMMANDS"]
    if kind == "stop":
        return _split_keywords(cfg.stop_keywords)
    if kind == "start":
        return _split_keywords(cfg.start_keywords)
    if kind == "symptom_check":
        return _split_keywords(cfg.symptom_check_keywords)
    return []


def _parse_iso_date(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    return date.fromisoformat(d)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _normalize_full_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    s = " ".join(str(name).strip().split())
    if not s:
        return None
    return s[:255]


def upsert_user(
    *, phone_e164: str, channel: Channel, language: Language, full_name: Optional[str] = None
) -> str:
    phone = normalize_phone_e164(phone_e164)
    name = _normalize_full_name(full_name)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if user:
            user.channel = channel
            user.language = language
            if name:
                user.full_name = name
            user.updated_at = now
            return user.id

        user_id = new_id("usr")
        s.add(
            User(
                id=user_id,
                phone_e164=phone,
                full_name=name,
                channel=channel,
                language=language,
                created_at=now,
                updated_at=now,
            )
        )
        return user_id


def set_language(*, phone_e164: str, language: Language) -> None:
    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    with session_scope() as s:
        s.execute(update(User).where(User.phone_e164 == phone).values(language=language, updated_at=now))


def get_user_language(phone_e164: str) -> Language:
    phone = normalize_phone_e164(phone_e164)
    with session_scope() as s:
        row = s.execute(select(User.language).where(User.phone_e164 == phone)).scalar_one_or_none()
    if row in ("en", "pcm", "ha", "ig", "yo"):
        return row  # type: ignore[return-value]
    return "en"


def set_enrollment_status(*, phone_e164: str, status: Literal["active", "paused"]) -> None:
    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if not user:
            return
        s.execute(update(Enrollment).where(Enrollment.user_id == user.id).values(status=status, updated_at=now))


def enroll_programs(
    *,
    phone_e164: str,
    channel: Channel,
    language: Language,
    programs: list[Program],
    edd_iso: Optional[str],
    full_name: Optional[str] = None,
    recurring_messages_opt_in: Optional[bool] = None,
) -> tuple[str, bool]:
    """
    Enrol or update programmes for a phone number (one User row per E.164).

    Returns (user_id, newly_enrolled). newly_enrolled is False when every requested
    programme was already active — repeat JOIN / join form submits are no-ops.
    """
    user_id = upsert_user(
        phone_e164=phone_e164, channel=channel, language=language, full_name=full_name
    )
    now = now_iso()
    newly_enrolled = False
    with session_scope() as s:
        for p in programs:
            edd = edd_iso if p == "maternal_child" else None
            enr = s.execute(
                select(Enrollment).where(and_(Enrollment.user_id == user_id, Enrollment.program == p))
            ).scalar_one_or_none()
            if enr:
                if enr.status != "active":
                    enr.status = "active"
                    enr.updated_at = now
                    newly_enrolled = True
                if edd and not enr.edd:
                    enr.edd = edd
                    enr.updated_at = now
            else:
                s.add(
                    Enrollment(
                        id=new_id("enr"),
                        user_id=user_id,
                        program=p,
                        status="active",
                        edd=edd,
                        created_at=now,
                        updated_at=now,
                    )
                )
                newly_enrolled = True
        if recurring_messages_opt_in is not None:
            u = s.get(User, user_id)
            if u:
                u.recurring_messages_opt_in = bool(recurring_messages_opt_in)
                u.updated_at = now
    try:
        from app.ehr_client import resolve_ehr_patient

        resolve_ehr_patient(
            user_id=user_id,
            phone_e164=phone_e164,
            full_name=full_name,
            programs=[str(p) for p in programs],
            language=language,
        )
    except Exception:
        pass
    return user_id, newly_enrolled


def should_show_helpdesk_hint(*, phone_e164: str) -> bool:
    """True when the symptom-reply Helpdesk prompt has not been shown yet today (UTC)."""
    phone = normalize_phone_e164(phone_e164)
    today = _today_utc().isoformat()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if not user:
            return True
        return (user.last_helpdesk_hint_date or "") != today


def record_helpdesk_hint_shown(*, phone_e164: str, channel: Channel = "whatsapp") -> None:
    phone = normalize_phone_e164(phone_e164)
    today = _today_utc().isoformat()
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if user:
            user.last_helpdesk_hint_date = today
            user.updated_at = now
            return
        s.add(
            User(
                id=new_id("usr"),
                phone_e164=phone,
                channel=channel,
                language="en",
                last_helpdesk_hint_date=today,
                created_at=now,
                updated_at=now,
            )
        )


def set_recurring_programme_opt_in(*, phone_e164: str, enabled: bool) -> None:
    """WhatsApp-only recurring education tips; stored on User.recurring_messages_opt_in."""
    phone = normalize_phone_e164(phone_e164)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if not user:
            return
        user.recurring_messages_opt_in = enabled
        user.updated_at = now


def _program_priority(program: str) -> int:
    """Prefer maternal tips when multiple programmes compete for the single daily WhatsApp tip."""
    order = {"maternal_child": 0, "chronic": 1, "youth": 2, "kidney": 3}
    return order.get(str(program), 99)


def _user_has_recurring_outbox_today(s: Session, user_id: str, today: str) -> bool:
    row = s.execute(
        select(MessageOutbox.id)
        .where(
            and_(
                MessageOutbox.user_id == user_id,
                MessageOutbox.status.in_(["queued", "sent", "failed"]),
                MessageOutbox.broadcast_run_id.is_(None),
                func.substr(MessageOutbox.due_at, 1, 10) == today,
            )
        )
        .limit(1)
    ).first()
    return row is not None


def user_has_active_enrollment(*, phone_e164: str) -> bool:
    phone = normalize_phone_e164(phone_e164)
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if not user:
            return False
        row = s.execute(
            select(Enrollment.id)
            .where(Enrollment.user_id == user.id, Enrollment.status == "active")
            .limit(1)
        ).first()
        return row is not None


def record_inbound(*, channel: str, from_phone: str, to_phone: Optional[str], body: str, raw: dict[str, Any]) -> None:
    with session_scope() as s:
        s.add(
            InboundMessage(
                id=new_id("inb"),
                channel=channel,
                from_phone_e164=normalize_phone_e164(from_phone),
                to_phone_e164=normalize_phone_e164(to_phone or "") if to_phone else None,
                body=body,
                raw_json=safe_json_dumps(raw),
                created_at=now_iso(),
            )
        )


def _patient_messaging_channel(channel: str) -> MessagingChannel:
    if (channel or "").lower().strip() == "whatsapp":
        return "whatsapp"
    return "sms"


def _notify_helpdesk_staff_after_ticket(
    *,
    ticket_id: str,
    patient_phone_e164: str,
    channel: str,
    question: str,
) -> None:
    raw = (get_effective_integration().helpdesk_phone_e164 or "").strip()
    if not raw:
        return
    dest = normalize_phone_e164(normalize_at_phone_number(raw))
    patient = normalize_phone_e164(patient_phone_e164)
    if dest == patient:
        return
    out_ch = _patient_messaging_channel(channel)
    preview = " ".join(question.strip().split())[:500]
    body = (
        f"Kareconnect helpdesk ticket {ticket_id}\n"
        f"Channel: {out_ch.upper()}\n"
        f"Patient: {patient}\n"
        f"Message: {preview}\n"
        f"---\nReply on {out_ch.upper()} with:\nTKT {ticket_id} your reply here"
    )
    res = send_message(to_phone_e164=dest, body=body, channel=out_ch)
    if not res.ok:
        logging.getLogger(__name__).warning("helpdesk staff notify failed: %s", res.error)


def deliver_helpdesk_staff_reply(*, body: str, staff_inbound_channel: MessagingChannel) -> str:
    from app.translation import translate_staff_reply_to_patient

    m = re.match(r"^\s*TKT\s+(\S+)\s+(.+)$", body.strip(), re.IGNORECASE | re.DOTALL)
    if not m:
        return (
            "Staff reply format: TKT <ticket_id> your message "
            "(example: TKT tkt_abc123 Thanks, we can help Tuesday). "
            "Patients: use JOIN or type a health question in normal words — no ticket id needed."
        )
    ticket_id, reply_text = m.group(1).strip(), m.group(2).strip()
    if not reply_text:
        return "Add your reply after the ticket id."
    with session_scope() as s:
        t = s.get(HelpdeskTicket, ticket_id)
        if not t:
            return "Unknown ticket id. Check the TKT line from your alert."
        pat_ch = _patient_messaging_channel(t.channel)
        if pat_ch != staff_inbound_channel:
            return (
                f"This ticket was opened on {pat_ch.upper()}. "
                "Send your TKT reply from that channel (same as the patient)."
            )
        if t.status == "closed":
            return "That ticket is closed."
        patient = t.from_phone_e164
        patient_lang: Language = "en"
        if t.user_id:
            u = s.get(User, t.user_id)
            if u and u.language in ("en", "pcm", "ha", "ig", "yo"):
                patient_lang = u.language  # type: ignore[assignment]
        else:
            u2 = s.execute(select(User).where(User.phone_e164 == patient)).scalar_one_or_none()
            if u2 and u2.language in ("en", "pcm", "ha", "ig", "yo"):
                patient_lang = u2.language  # type: ignore[assignment]
    outbound = translate_staff_reply_to_patient(reply_text, patient_lang)
    res = send_message(to_phone_e164=patient, body=outbound, channel=pat_ch)
    if not res.ok:
        return f"Could not send to patient: {res.error or 'error'}"
    with session_scope() as s:
        t2 = s.get(HelpdeskTicket, ticket_id)
        if t2:
            t2.status = "answered"
            t2.updated_at = now_iso()
    return "Sent to patient."


def _notify_operator_new_ticket(
    *,
    ticket_id: str,
    from_phone_e164: str,
    channel: str,
    question: str,
    suggested_answer: Optional[str],
    karevault_ai_reply: Optional[str],
) -> None:
    log = logging.getLogger(__name__)
    try:
        from app.integration_config import get_effective_integration

        integ = get_effective_integration()
        url = (integ.operator_webhook_url or "").strip()
        if not url:
            return
        base = (integ.hc_base_url or "").rstrip("/")
        admin_link = f"{base}/admin/helpdesk-ticket/details/{ticket_id}"
        payload = {
            "event": "helpdesk.ticket.created",
            "ticket_id": ticket_id,
            "admin_url": admin_link,
            "from_phone_e164": from_phone_e164,
            "channel": channel,
            "question": question,
            "suggested_answer": suggested_answer,
        }
        if karevault_ai_reply is not None:
            payload["karevault_ai_reply"] = karevault_ai_reply
        r = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not (200 <= r.status_code < 300):
            log.warning("operator_webhook HTTP %s: %s", r.status_code, (r.text or "")[:500])
    except Exception as exc:
        log.warning("operator_webhook failed: %s", exc)


def open_helpdesk_ticket(
    *,
    from_phone: str,
    channel: str,
    question: str,
    suggested_answer: Optional[str],
    ai_symptom_reply: Optional[str] = None,
) -> str:
    phone = normalize_phone_e164(from_phone)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        ticket_id = new_id("tkt")
        s.add(
            HelpdeskTicket(
                id=ticket_id,
                user_id=user.id if user else None,
                from_phone_e164=phone,
                channel=channel,
                question=question,
                status="open",
                suggested_answer=suggested_answer,
                ai_symptom_reply=ai_symptom_reply,
                created_at=now,
                updated_at=now,
            )
        )
    _notify_operator_new_ticket(
        ticket_id=ticket_id,
        from_phone_e164=phone,
        channel=channel,
        question=question,
        suggested_answer=suggested_answer,
        karevault_ai_reply=ai_symptom_reply,
    )
    _notify_helpdesk_staff_after_ticket(
        ticket_id=ticket_id,
        patient_phone_e164=phone,
        channel=channel,
        question=question,
    )
    return ticket_id


def request_appointment(
    *,
    from_phone: str,
    reason: str,
    preferred_date: Optional[str] = None,
    visit_type: str = "in_person",
    channel: Optional[str] = None,
) -> str:
    phone = normalize_phone_e164(from_phone)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        appt_id = new_id("apt")
        s.add(
            Appointment(
                id=appt_id,
                user_id=user.id if user else None,
                phone_e164=phone,
                channel=channel,
                visit_type=visit_type,
                reason=reason,
                preferred_date=preferred_date,
                status="requested",
                ehr_sync_status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    from app.ehr_client import sync_appointment_to_ehr

    sync_appointment_to_ehr(appt_id)
    return appt_id


def request_escalation(*, from_phone: str, reason: str, urgency: str) -> str:
    phone = normalize_phone_e164(from_phone)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        esc_id = new_id("esc")
        s.add(
            Escalation(
                id=esc_id,
                user_id=user.id if user else None,
                phone_e164=phone,
                reason=reason,
                urgency=urgency,
                status="requested",
                created_at=now,
                updated_at=now,
            )
        )
    return esc_id


def request_rpm(*, from_phone: str, condition: str, device_type: Optional[str]) -> str:
    phone = normalize_phone_e164(from_phone)
    now = now_iso()
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        rpm_id = new_id("rpm")
        s.add(
            RpmEnrollment(
                id=rpm_id,
                user_id=user.id if user else None,
                phone_e164=phone,
                condition=condition,
                device_type=device_type,
                status="requested",
                created_at=now,
                updated_at=now,
            )
        )
    return rpm_id


def _load_program_content() -> dict[str, Any]:
    fp = Path(__file__).parent / "content" / "program_messages.json"
    return json.loads(fp.read_text(encoding="utf-8"))


def _maternal_key_for_today(edd: date, today: date) -> str:
    weeks_to_edd = int((edd - today).days // 7)
    # pick the closest key we have: -32,-24,-16,-8,-2,+1
    candidates = [-32, -24, -16, -8, -2, 1]
    closest = min(candidates, key=lambda k: abs(k - weeks_to_edd))
    return f"{closest:+d}".replace("+", "+")


def _count_sent_program_outbox(s: Session, user_id: str, program: str) -> int:
    return int(
        s.execute(
            select(func.count())
            .select_from(MessageOutbox)
            .where(
                and_(
                    MessageOutbox.user_id == user_id,
                    MessageOutbox.program == program,
                    MessageOutbox.status == "sent",
                )
            )
        ).scalar_one()
    )


def _content_for_enrollment(
    s: Session,
    user_id: str,
    program: Program,
    language: Language,
    edd_iso: Optional[str],
) -> Optional[str]:
    content = _load_program_content()
    today = _today_utc()

    prog = content.get(program)
    if not prog:
        return None

    messages_by_lang: dict[str, dict[str, str]] = prog["messages"]
    lang = language if language in messages_by_lang else "en"
    messages = messages_by_lang.get(lang, {})

    if program == "maternal_child":
        edd = _parse_iso_date(edd_iso)
        if not edd:
            # No EDD: do not assume pregnancy — rotate general maternal tips only (EDD can be added on web join).
            en = messages_by_lang.get("en", {})
            no_edd_keys = sorted(
                (k for k in en if str(k).startswith("noedd_")),
                key=lambda x: int(str(x).replace("noedd_", "")),
            )
            prior_sent = _count_sent_program_outbox(s, user_id, "maternal_child")
            if no_edd_keys:
                k = no_edd_keys[prior_sent % len(no_edd_keys)]
                return en.get(k) or messages_by_lang.get("en", {}).get("-16")
            return messages_by_lang.get("en", {}).get("-16")
        key = _maternal_key_for_today(edd, today)
        # keys in JSON include "-16" and "+1" and also may include "0"
        if key in messages:
            return messages[key]
        # fallback
        return messages_by_lang.get("en", {}).get("-16")

    # non-maternal: cycle weekly based on enrollment age using first-created message count
    # We treat keys as "0","1","2"... (string)
    if not messages:
        return None
    keys = sorted(messages.keys(), key=lambda x: int(x))
    idx = int(today.toordinal()) % len(keys)
    return messages[keys[idx]]


def enqueue_due_messages() -> int:
    """
    Recurring programme education messages (smart content from program_messages.json):
    maternal uses EDD-aware keys when available; other programmes rotate content by calendar day.

    Rules:
    - Only users who opted in (User.recurring_messages_opt_in).
    - WhatsApp delivery only (channel forced to whatsapp on the outbox row).
    - At most one recurring tip per user per calendar day; if enrolled in multiple programmes,
      maternal_child is preferred, then chronic, youth, kidney.
    """
    now = now_iso()
    today = _today_utc().isoformat()
    created = 0
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Enrollment, User)
                .join(User, User.id == Enrollment.user_id)
                .where(
                    Enrollment.status == "active",
                    User.channel == "whatsapp",
                    User.recurring_messages_opt_in.is_(True),
                )
            ).all()
        )
        rows.sort(key=lambda pair: (pair[1].id, _program_priority(pair[0].program)))

        fulfilled_users: set[str] = set()
        for enr, user in rows:
            if user.id in fulfilled_users:
                continue
            if _user_has_recurring_outbox_today(s, user.id, today):
                fulfilled_users.add(user.id)
                continue
            # Join welcome is sent separately; skip programme tips on first enrolment day.
            if (enr.created_at or "")[:10] == today:
                fulfilled_users.add(user.id)
                continue

            body = _content_for_enrollment(s, user.id, enr.program, user.language, enr.edd)
            if not body:
                continue

            s.add(
                MessageOutbox(
                    id=new_id("msg"),
                    user_id=user.id,
                    program=enr.program,
                    channel="whatsapp",
                    language=user.language,
                    to_phone_e164=user.phone_e164,
                    body=body,
                    due_at=now,
                    status="queued",
                    error=None,
                    created_at=now,
                    sent_at=None,
                )
            )
            fulfilled_users.add(user.id)
            created += 1

    return created


def send_queued_messages(limit: int = 50) -> int:
    from app.broadcast import outbound_body_for_outbox_send

    now = now_iso()
    sent = 0
    with session_scope() as s:
        rows = (
            s.execute(
                select(MessageOutbox)
                .where(
                    and_(
                        MessageOutbox.status == "queued",
                        MessageOutbox.due_at <= now,
                        MessageOutbox.broadcast_run_id.is_(None),
                    )
                )
                .order_by(
                    case((MessageOutbox.broadcast_run_id.isnot(None), 0), else_=1),
                    MessageOutbox.due_at.asc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        for msg in rows:
            body = outbound_body_for_outbox_send(s, msg)
            res = send_message(to_phone_e164=msg.to_phone_e164, body=body, channel=msg.channel)  # type: ignore[arg-type]
            if res.ok:
                msg.status = "sent"
                msg.sent_at = now
                msg.error = None
                sent += 1
            else:
                msg.status = "failed"
                msg.error = res.error
    return sent


def active_program_codes_for_phone_e164(phone_e164: str) -> list[str]:
    phone = normalize_phone_e164(phone_e164)
    with session_scope() as s:
        user = s.execute(select(User).where(User.phone_e164 == phone)).scalar_one_or_none()
        if not user:
            return []
        rows = (
            s.execute(
                select(Enrollment.program)
                .where(Enrollment.user_id == user.id, Enrollment.status == "active")
                .distinct()
            )
            .scalars()
            .all()
        )
        return [str(p) for p in rows]


def handle_symptom_question(
    *,
    from_phone: str,
    question: str,
    question_english: Optional[str] = None,
    programs: Optional[list[str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """FAQ match + KareVault symptom checker. Does not open a helpdesk ticket."""
    q_en = (question_english or "").strip() or question
    faqs = load_faqs()
    suggested = best_faq_answer(q_en, faqs)
    program_context = programs if programs is not None else active_program_codes_for_phone_e164(from_phone)
    kv = fetch_symptom_checker_reply(question=q_en, programs=program_context)
    return suggested, kv


def handle_helpdesk_question(
    *,
    from_phone: str,
    channel: str,
    question: str,
    question_english: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Opens a helpdesk ticket. `question` is stored as the patient wrote it (after Helpdesk prefix)."""
    q_en = (question_english or "").strip() or question
    faqs = load_faqs()
    suggested = best_faq_answer(q_en, faqs)
    ticket_id = open_helpdesk_ticket(
        from_phone=from_phone,
        channel=channel,
        question=question,
        suggested_answer=suggested,
        ai_symptom_reply=None,
    )
    return ticket_id, suggested

