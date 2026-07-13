"""Interactive APPOINT command: clinic visits + video telesessions with availability checks."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.ehr_client import (
    fetch_ehr_appointment_availability,
    resolve_ehr_patient,
    sync_appointment_to_ehr,
)
from app.integration_config import get_effective_integration
from app.messaging import send_message
from app.models import (
    Appointment,
    AppointmentPatientFlow,
    TelehealthProvider,
    TelehealthSession,
    User,
)
from app.services import active_program_codes_for_phone_e164
from app.telehealth import (
    _available_providers_stmt,
    _busy_provider_ids,
    _provider_rating_summary,
    finalize_new_booking,
    format_fee,
    list_professions_with_counts,
    new_booking_session_status,
    profession_label,
    provider_availability_label,
)
from app.util import new_id, now_iso, normalize_phone_e164

_log = logging.getLogger(__name__)

FlowStep = Literal[
    "idle",
    "visit_type",
    "reason",
    "profession",
    "provider",
    "confirm",
    "preferred_date",
    "clinic_confirm",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_PREF_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2}))?$")
_CLINIC_DATE_MENU_DAYS = 5


def _get_or_create_user_id(s: Session, phone_e164: str) -> Optional[str]:
    return s.execute(select(User.id).where(User.phone_e164 == phone_e164)).scalar_one_or_none()


def _get_flow(s: Session, phone_e164: str, channel: str) -> AppointmentPatientFlow:
    row = s.execute(
        select(AppointmentPatientFlow).where(AppointmentPatientFlow.phone_e164 == phone_e164)
    ).scalar_one_or_none()
    if row:
        row.channel = channel
        return row
    row = AppointmentPatientFlow(
        id=new_id("apf"),
        phone_e164=phone_e164,
        user_id=_get_or_create_user_id(s, phone_e164),
        channel=channel,
        step="idle",
        visit_type=None,
        reason=None,
        profession_code=None,
        selected_provider_id=None,
        preferred_date=None,
        pending_appointment_id=None,
        updated_at=now_iso(),
    )
    s.add(row)
    s.flush()
    return row


def _reset_flow(flow: AppointmentPatientFlow) -> None:
    flow.step = "idle"
    flow.visit_type = None
    flow.reason = None
    flow.profession_code = None
    flow.selected_provider_id = None
    flow.preferred_date = None
    flow.pending_appointment_id = None
    flow.updated_at = now_iso()


def cancel_appointment_flow(phone_e164: str) -> None:
    with session_scope() as s:
        flow = s.execute(
            select(AppointmentPatientFlow).where(AppointmentPatientFlow.phone_e164 == phone_e164)
        ).scalar_one_or_none()
        if flow:
            _reset_flow(flow)


def is_in_appointment_flow(phone_e164: str) -> bool:
    with session_scope() as s:
        step = s.execute(
            select(AppointmentPatientFlow.step).where(AppointmentPatientFlow.phone_e164 == phone_e164)
        ).scalar_one_or_none()
    return bool(step and step != "idle")


def _availability_summary() -> str:
    items = list_professions_with_counts()
    available = sum(int(i.get("count") or 0) for i in items)
    busy = sum(int(i.get("busy") or 0) for i in items)
    if available:
        return f"Video clinicians available now: {available} ({busy} busy)."
    if busy:
        return f"All video clinicians are busy ({busy}). You can still request a clinic visit."
    return "No video clinicians listed right now. You can still request a clinic visit."


def _visit_type_menu(*, reason_hint: Optional[str]) -> str:
    lines = ["Appointment booking — what do you need?"]
    if reason_hint:
        lines.append(f"Reason: {reason_hint[:120]}")
    lines.append(_availability_summary())
    lines.extend(
        [
            "",
            "1. Video consultation (telesession — we check clinician availability now)",
            "2. Clinic visit (choose a preferred date)",
            "",
            "Reply 1 or 2, or CANCEL to exit.",
        ]
    )
    return "\n".join(lines)


def _profession_menu_text() -> str:
    items = list_professions_with_counts()
    available = [item for item in items if item["count"] > 0]
    if not available:
        busy_total = sum(item.get("busy", 0) for item in items)
        if busy_total:
            return (
                "All video clinicians are busy right now. Reply 2 to switch to a clinic visit, "
                "or try again later. Reply CANCEL to exit."
            )
        return (
            "No video clinicians are available. Reply 2 for a clinic visit or CANCEL to exit."
        )
    lines = ["Video appointment — choose a specialty (reply with the number):"]
    for i, item in enumerate(available, start=1):
        suffix = f" ({item['count']} available now)"
        if item.get("busy"):
            suffix += f" — {item['busy']} busy"
        lines.append(f"{i}. {item['label']}{suffix}")
    lines.append("\nReply CANCEL to exit appointment booking.")
    return "\n".join(lines)


def _provider_menu_text(s: Session, profession: str) -> str:
    providers = s.execute(_available_providers_stmt(s, profession)).scalars().all()
    if not providers:
        busy = len(_busy_provider_ids(s, profession))
        if busy:
            return (
                f"All {profession_label(profession)} clinicians are busy ({busy}). "
                "Try again in a few minutes, reply BACK, or CANCEL."
            )
        return f"No providers for {profession_label(profession)}. Reply BACK or CANCEL."
    lines = [f"Video appointment — {profession_label(profession)} (reply with the number):"]
    for i, p in enumerate(providers, start=1):
        avg, cnt = _provider_rating_summary(s, p.id)
        rating = f"★ {avg:.1f} ({cnt})" if cnt else "No ratings yet"
        fee = format_fee(p.fee_ngn, p.currency)
        title = (p.title or "").strip()
        name = (p.full_name or "").strip()
        display = f"{title} {name}".strip() if title else name
        avail = provider_availability_label(s, p.id)
        avail_tag = " — available now" if avail == "free" else ""
        lines.append(f"{i}. {display} — {fee} — {rating}{avail_tag}")
    lines.append("\nReply BACK for specialties or CANCEL to exit.")
    return "\n".join(lines)


def _confirm_text(s: Session, provider: TelehealthProvider, reason: str) -> str:
    fee = format_fee(provider.fee_ngn, provider.currency)
    avg, cnt = _provider_rating_summary(s, provider.id)
    rating = f"Average rating: {avg:.1f}/5 ({cnt} reviews)" if cnt else "No ratings yet"
    name = ((provider.title or "") + " " + (provider.full_name or "")).strip()
    avail = provider_availability_label(s, provider.id)
    if avail != "free":
        return (
            f"{name} is no longer free ({avail}). "
            f"{_provider_menu_text(s, provider.profession)}"
        )
    return (
        f"Confirm video appointment:\n"
        f"Reason: {reason[:200]}\n"
        f"Provider: {name} ({profession_label(provider.profession)})\n"
        f"Consultation fee: {fee}\n"
        f"{rating}\n\n"
        "Reply YES to book and receive call/payment instructions.\n"
        "Reply BACK to choose another provider or CANCEL to exit."
    )


def _clinic_confirm_text(*, reason: str, preferred_date: str, slot_msg: str) -> str:
    extra = f"\n{slot_msg}" if slot_msg else ""
    return (
        f"Confirm clinic appointment:\n"
        f"Reason: {reason[:200]}\n"
        f"Preferred date: {preferred_date}{extra}\n\n"
        "Reply YES to submit. Our team will confirm your slot.\n"
        "Reply BACK to change the date or CANCEL to exit."
    )


def _parse_date_iso(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not _DATE_RE.match(t):
        return None
    try:
        d = date.fromisoformat(t)
    except ValueError:
        return None
    if d < datetime.now(timezone.utc).date():
        return None
    return t


def _next_weekdays(count: int = _CLINIC_DATE_MENU_DAYS) -> list[date]:
    """Upcoming weekdays (Mon–Fri) starting tomorrow."""
    out: list[date] = []
    cursor = datetime.now(timezone.utc).date() + timedelta(days=1)
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _clinic_date_menu_text() -> str:
    lines = [
        "Choose a clinic date (reply with the number), or send your preferred date and time:",
        "",
    ]
    for i, d in enumerate(_next_weekdays(), start=1):
        lines.append(f"{i}. {d.strftime('%a %d %b %Y')} ({d.isoformat()})")
    lines.extend(
        [
            "",
            "Or reply with YYYY-MM-DD, optionally with time HH:MM (e.g. 2026-06-20 14:30).",
            "Reply BACK or CANCEL to exit.",
        ]
    )
    return "\n".join(lines)


def _split_preferred_date_time(raw: str) -> tuple[str, str]:
    """Extract YYYY-MM-DD and HH:MM from preferred_date or scheduled_at fragments."""
    text = (raw or "").strip()
    if not text:
        return "", "09:00"
    if len(text) >= 16 and text[10] in ("T", " "):
        date_part = text[:10]
        time_part = text[11:16]
        if _DATE_RE.match(date_part) and len(time_part) == 5 and time_part[2] == ":":
            return date_part, time_part
    if _DATE_RE.match(text[:10]):
        return text[:10], "09:00"
    return "", "09:00"


def _parse_datetime_preference(text: str) -> Optional[tuple[str, Optional[str]]]:
    """Parse YYYY-MM-DD or YYYY-MM-DD HH:MM (future dates only)."""
    t = (text or "").strip()
    m = _DATETIME_PREF_RE.match(t)
    if not m:
        return None
    date_part = m.group(1)
    try:
        d = date.fromisoformat(date_part)
    except ValueError:
        return None
    if d < datetime.now(timezone.utc).date():
        return None
    if m.group(2) is not None:
        hh = int(m.group(2))
        mm = int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return date_part, f"{hh:02d}:{mm:02d}"
    return date_part, None


def _default_scheduled_from_appointment(appt: Appointment) -> tuple[str, str]:
    if appt.scheduled_at and len(appt.scheduled_at) >= 10:
        return appt.scheduled_at[:10], appointment_scheduled_time(appt) or "09:00"
    if appt.preferred_date:
        d, t = _split_preferred_date_time(appt.preferred_date)
        if d:
            return d, t
    weekdays = _next_weekdays(1)
    fallback = weekdays[0] if weekdays else datetime.now(timezone.utc).date()
    return fallback.isoformat(), "09:00"


def _ensure_ehr_patient(phone_e164: str, user_id: Optional[str]) -> None:
    if not user_id:
        return
    try:
        resolve_ehr_patient(
            user_id=user_id,
            phone_e164=phone_e164,
            programs=active_program_codes_for_phone_e164(phone_e164),
        )
    except Exception:
        _log.exception("EHR patient resolve failed during appointment flow phone=%s", phone_e164)


def _notify_helpdesk_appointment(appointment_id: str, *, event: str = "booked") -> None:
    cfg = get_effective_integration()
    dest = (cfg.helpdesk_phone_e164 or "").strip()
    if not dest:
        return
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return
        ref = appointment_id.replace("apt_", "APT-")[:16].upper()
        visit = "Video" if appt.visit_type == "telehealth" else "Clinic"
        lines = [
            f"Kareconnect appointment {event} ({ref})",
            f"Patient: {appt.phone_e164}",
            f"Type: {visit}",
            f"Reason: {(appt.reason or '')[:200]}",
        ]
        if appt.preferred_date:
            lines.append(f"Preferred date: {appt.preferred_date}")
        if appt.telehealth_session_id:
            lines.append(f"Telehealth session: {appt.telehealth_session_id}")
        if appt.ehr_appointment_id:
            lines.append(f"EHR id: {appt.ehr_appointment_id}")
        body = "\n".join(lines)
        channel = appt.channel or "whatsapp"
    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    res = send_message(to_phone_e164=dest, body=body, channel=ch)
    if not res.ok:
        _log.warning("helpdesk appointment notify failed: %s", res.error)


def _book_clinic_appointment(
    s: Session,
    *,
    flow: AppointmentPatientFlow,
    phone_e164: str,
    channel: str,
) -> str:
    reason = (flow.reason or "General appointment").strip()
    preferred = flow.preferred_date or ""
    user_id = flow.user_id or _get_or_create_user_id(s, phone_e164)
    now = now_iso()
    appt_id = new_id("apt")
    scheduled_at: Optional[str] = None
    if preferred:
        date_part, time_part = _split_preferred_date_time(preferred)
        if date_part:
            scheduled_at = f"{date_part}T{time_part}:00+00:00"
    s.add(
        Appointment(
            id=appt_id,
            user_id=user_id,
            phone_e164=phone_e164,
            channel=channel,
            visit_type="in_person",
            reason=reason,
            preferred_date=preferred,
            scheduled_at=scheduled_at,
            status="requested",
            ehr_sync_status="pending",
            created_at=now,
            updated_at=now,
        )
    )
    flow.pending_appointment_id = appt_id
    _reset_flow(flow)
    s.flush()

    sync_appointment_to_ehr(appt_id)
    _notify_helpdesk_appointment(appt_id, event="requested")
    ref = appt_id.replace("apt_", "APT-")[:16].upper()
    return (
        f"Clinic appointment requested (ref {ref}) for {preferred}. "
        "Our team will confirm your slot. "
        "Send MENU for commands."
    )


def _book_video_appointment(
    s: Session,
    *,
    flow: AppointmentPatientFlow,
    phone_e164: str,
    channel: str,
) -> str:
    provider = s.get(TelehealthProvider, flow.selected_provider_id)
    if not provider or provider.status != "active":
        _reset_flow(flow)
        return "That clinician is no longer available. Send APPOINT to try again."
    if provider_availability_label(s, provider.id) != "free":
        flow.step = "provider"
        flow.selected_provider_id = None
        flow.updated_at = now_iso()
        return (
            f"{provider.full_name} is no longer free. "
            f"{_provider_menu_text(s, flow.profession_code or provider.profession)}"
        )

    reason = (flow.reason or "Video consultation").strip()
    user_id = flow.user_id or _get_or_create_user_id(s, phone_e164)
    now = now_iso()
    appt_id = new_id("apt")
    session_id = new_id("ths")
    s.add(
        Appointment(
            id=appt_id,
            user_id=user_id,
            phone_e164=phone_e164,
            channel=channel,
            visit_type="telehealth",
            reason=reason,
            profession=provider.profession,
            provider_id=provider.id,
            telehealth_session_id=session_id,
            status="booked",
            ehr_sync_status="pending",
            scheduled_at=now,
            created_at=now,
            updated_at=now,
        )
    )
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
    flow.pending_appointment_id = appt_id
    _reset_flow(flow)
    s.flush()

    sync_appointment_to_ehr(appt_id)
    _notify_helpdesk_appointment(appt_id, event="booked")
    return finalize_new_booking(session_id, db=s)


def start_appointment_flow(
    *,
    phone_e164: str,
    channel: Literal["sms", "whatsapp"],
    reason: Optional[str] = None,
) -> str:
    from app.telehealth import cancel_telehealth_flow

    phone = normalize_phone_e164(phone_e164)
    cancel_telehealth_flow(phone)
    reason_clean = (reason or "").strip() or None
    with session_scope() as s:
        flow = _get_flow(s, phone, channel)
        flow.step = "visit_type"
        flow.reason = reason_clean
        flow.visit_type = None
        flow.profession_code = None
        flow.selected_provider_id = None
        flow.preferred_date = None
        flow.updated_at = now_iso()
        if flow.user_id:
            _ensure_ehr_patient(phone, flow.user_id)
    return _visit_type_menu(reason_hint=reason_clean)


def appointment_ref(appointment_id: str) -> str:
    return (appointment_id or "").replace("apt_", "APT-")[:16].upper() or "—"


def appointment_calendar_date(appt: Appointment) -> Optional[str]:
    if appt.scheduled_at and len(appt.scheduled_at) >= 10:
        return appt.scheduled_at[:10]
    if appt.preferred_date and len(appt.preferred_date) >= 10:
        return appt.preferred_date[:10]
    return None


def appointment_scheduled_time(appt: Appointment) -> Optional[str]:
    """HH:MM from scheduled_at, if present."""
    raw = (appt.scheduled_at or "").strip()
    if len(raw) >= 16 and raw[10] == "T":
        return raw[11:16]
    return None


def _appointment_event_dict(appt: Appointment, providers: dict[str, TelehealthProvider]) -> dict[str, Any]:
    prov = providers.get(appt.provider_id) if appt.provider_id else None
    prov_name = f"{prov.title or ''} {prov.full_name or ''}".strip() if prov else ""
    cal_date = appointment_calendar_date(appt)
    return {
        "id": appt.id,
        "ref": appointment_ref(appt.id),
        "phone": appt.phone_e164,
        "visit_type": appt.visit_type,
        "visit_label": "Video" if appt.visit_type == "telehealth" else "Clinic",
        "status": appt.status,
        "reason": (appt.reason or "")[:120],
        "date": cal_date,
        "time": appointment_scheduled_time(appt),
        "preferred_date": appt.preferred_date,
        "scheduled_at": appt.scheduled_at,
        "provider_id": appt.provider_id,
        "provider_name": prov_name or "—",
        "telehealth_session_id": appt.telehealth_session_id,
        "ehr_sync_status": appt.ehr_sync_status,
        "created_at": appt.created_at,
        "can_assign": appt.status == "requested",
        "detail_url": f"/admin/appointment/details/{appt.id}",
        "assign_url": f"/admin/appointment/assign?pk={appt.id}",
        "draggable": appt.status != "cancelled",
    }


def _load_providers_map(provider_ids: set[str]) -> dict[str, TelehealthProvider]:
    if not provider_ids:
        return {}
    with session_scope() as s:
        return {
            p.id: p
            for p in s.execute(
                select(TelehealthProvider).where(TelehealthProvider.id.in_(provider_ids))
            ).scalars().all()
        }


def list_appointment_events(*, start: date, end: date) -> list[dict[str, Any]]:
    """Appointments with a calendar date in [start, end] (non-cancelled)."""
    events: list[dict[str, Any]] = []
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Appointment)
                .where(Appointment.status != "cancelled")
                .order_by(Appointment.scheduled_at.asc().nulls_last(), Appointment.created_at.asc())
            ).scalars().all()
        )
        provider_ids = {a.provider_id for a in rows if a.provider_id}
        providers: dict[str, TelehealthProvider] = {}
        if provider_ids:
            providers = {
                p.id: p
                for p in s.execute(
                    select(TelehealthProvider).where(TelehealthProvider.id.in_(provider_ids))
                ).scalars().all()
            }
        for appt in rows:
            ev = _appointment_event_dict(appt, providers)
            d = ev.get("date")
            if not d:
                continue
            try:
                dt = date.fromisoformat(d[:10])
            except ValueError:
                continue
            if start <= dt <= end:
                events.append(ev)
    return events


def list_pending_appointments() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Appointment)
                .where(Appointment.status == "requested")
                .order_by(Appointment.created_at.desc())
            ).scalars().all()
        )
        provider_ids = {a.provider_id for a in rows if a.provider_id}
        providers: dict[str, TelehealthProvider] = {}
        if provider_ids:
            providers = {
                p.id: p
                for p in s.execute(
                    select(TelehealthProvider).where(TelehealthProvider.id.in_(provider_ids))
                ).scalars().all()
            }
        return [_appointment_event_dict(a, providers) for a in rows]


def calendar_range_for_view(view: str, anchor: date) -> tuple[date, date]:
    v = (view or "month").lower()
    if v == "day":
        return anchor, anchor
    if v == "week":
        week_start = anchor - timedelta(days=anchor.weekday())
        return week_start, week_start + timedelta(days=6)
    # month — grid covers full weeks visible in month
    first = date(anchor.year, anchor.month, 1)
    grid_start = first - timedelta(days=first.weekday())
    grid_end = grid_start + timedelta(days=41)
    return grid_start, grid_end


def _nav_for_view(view: str, anchor: date) -> dict[str, Any]:
    v = (view or "month").lower()
    if v == "day":
        prev_d = anchor - timedelta(days=1)
        next_d = anchor + timedelta(days=1)
        label = anchor.strftime("%A, %d %B %Y")
    elif v == "week":
        week_start = anchor - timedelta(days=anchor.weekday())
        week_end = week_start + timedelta(days=6)
        prev_d = week_start - timedelta(days=7)
        next_d = week_start + timedelta(days=7)
        label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
    else:
        first = date(anchor.year, anchor.month, 1)
        prev_m = date(anchor.year - 1, 12, 1) if anchor.month == 1 else date(anchor.year, anchor.month - 1, 1)
        next_m = date(anchor.year + 1, 1, 1) if anchor.month == 12 else date(anchor.year, anchor.month + 1, 1)
        prev_d = prev_m
        next_d = next_m
        label = anchor.strftime("%B %Y")
    today = datetime.now(timezone.utc).date()
    return {
        "label": label,
        "prev_date": prev_d.isoformat(),
        "next_date": next_d.isoformat(),
        "today_date": today.isoformat(),
        "anchor_date": anchor.isoformat(),
    }


def load_calendar_page_context(*, view: str, anchor: date) -> dict[str, Any]:
    v = view if view in ("day", "week", "month") else "month"
    start, end = calendar_range_for_view(v, anchor)
    events = list_appointment_events(start=start, end=end)
    pending = list_pending_appointments()
    nav = _nav_for_view(v, anchor)
    return {
        "view": v,
        "anchor_date": anchor.isoformat(),
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "nav": nav,
        "events": events,
        "pending": pending,
        "pending_count": len(pending),
        "event_count": len(events),
    }


def load_appointment_calendar(*, year: int, month: int) -> dict[str, Any]:
    """Legacy month loader — delegates to load_calendar_page_context."""
    anchor = date(year, month, 15)
    ctx = load_calendar_page_context(view="month", anchor=anchor)
    # shape compatible with old template fields where needed
    month_names = [
        "",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1
    return {
        "year": year,
        "month": month,
        "month_label": f"{month_names[month]} {year}",
        "prev": {"year": prev_y, "month": prev_m},
        "next": {"year": next_y, "month": next_m},
        "days": [],
        "pending": ctx["pending"],
        "unscheduled": [],
        "total_in_month": ctx["event_count"],
        "pending_count": ctx["pending_count"],
        "page": ctx,
    }


def list_active_providers_for_assign(*, profession: Optional[str] = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        stmt = select(TelehealthProvider).where(TelehealthProvider.status == "active")
        if profession:
            stmt = stmt.where(TelehealthProvider.profession == profession)
        providers = s.execute(stmt.order_by(TelehealthProvider.full_name.asc())).scalars().all()
        out: list[dict[str, Any]] = []
        for p in providers:
            avail = provider_availability_label(s, p.id)
            title = (p.title or "").strip()
            name = (p.full_name or "").strip()
            display = f"{title} {name}".strip() if title else name
            out.append(
                {
                    "id": p.id,
                    "label": (
                        f"{display} — {profession_label(p.profession)} "
                        f"({format_fee(p.fee_ngn, p.currency)}) — {avail}"
                    ),
                    "availability": avail,
                    "profession": p.profession,
                }
            )
    return out


def get_appointment_assign_context(appointment_id: str) -> Optional[dict[str, Any]]:
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return None
        prov = s.get(TelehealthProvider, appt.provider_id) if appt.provider_id else None
        sched_date, sched_time = _default_scheduled_from_appointment(appt)
        return {
            "appointment_id": appt.id,
            "ref": appointment_ref(appt.id),
            "phone": appt.phone_e164,
            "visit_type": appt.visit_type,
            "visit_label": "Video" if appt.visit_type == "telehealth" else "Clinic",
            "status": appt.status,
            "reason": appt.reason,
            "preferred_date": appt.preferred_date or "",
            "scheduled_date": sched_date,
            "scheduled_time": sched_time,
            "scheduled_at": appt.scheduled_at or "",
            "profession": appt.profession,
            "current_provider_id": appt.provider_id,
            "current_provider_name": (
                f"{prov.title or ''} {prov.full_name or ''}".strip() if prov else "—"
            ),
            "providers": list_active_providers_for_assign(profession=appt.profession),
        }


def notify_patient_appointment_confirmed(appointment_id: str) -> tuple[bool, str]:
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return False, "Appointment not found"
        prov = s.get(TelehealthProvider, appt.provider_id) if appt.provider_id else None
        phone = appt.phone_e164
        channel = appt.channel or "whatsapp"
        ref = appointment_ref(appt.id)
        visit = "video consultation" if appt.visit_type == "telehealth" else "clinic visit"
        when = ""
        if appt.scheduled_at:
            when = appt.scheduled_at.replace("T", " ")[:16]
        elif appt.preferred_date:
            when = appt.preferred_date
        body = (
            f"Your Kareconnect {visit} is confirmed (ref {ref}).\n"
            f"Reason: {(appt.reason or '')[:200]}"
        )
        if when:
            body += f"\nWhen: {when}"
        if prov:
            body += f"\nClinician: {(prov.title or '')} {prov.full_name}".strip()
        body += "\n\nReply MENU for commands."

    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    res = send_message(to_phone_e164=phone, body=body, channel=ch)
    if not res.ok:
        return False, res.error or "send failed"
    return True, f"Confirmation sent on {ch.upper()}"


def notify_patient_appointment_rescheduled(appointment_id: str) -> tuple[bool, str]:
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return False, "Appointment not found"
        prov = s.get(TelehealthProvider, appt.provider_id) if appt.provider_id else None
        phone = appt.phone_e164
        channel = appt.channel or "whatsapp"
        ref = appointment_ref(appt.id)
        visit = "video consultation" if appt.visit_type == "telehealth" else "clinic visit"
        when = ""
        if appt.scheduled_at:
            when = appt.scheduled_at.replace("T", " ")[:16]
        elif appt.preferred_date:
            when = appt.preferred_date
        body = f"Your Kareconnect {visit} has been rescheduled (ref {ref})."
        if when:
            body += f"\nNew time: {when}"
        body += f"\nReason: {(appt.reason or '')[:200]}"
        if prov:
            body += f"\nClinician: {(prov.title or '')} {prov.full_name}".strip()
        body += "\n\nReply MENU for commands."

    ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
    res = send_message(to_phone_e164=phone, body=body, channel=ch)
    if not res.ok:
        return False, res.error or "send failed"
    return True, f"Reschedule notice sent on {ch.upper()}"


def admin_approve_and_assign_appointment(
    *,
    appointment_id: str,
    provider_id: str,
    scheduled_date: Optional[str] = None,
    scheduled_time: str = "09:00",
    notify_patient: bool = True,
) -> tuple[bool, str]:
    pid = (provider_id or "").strip()
    if not pid:
        return False, "Choose a provider."

    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return False, "Appointment not found"
        if appt.status == "cancelled":
            return False, "Cannot assign a cancelled appointment"
        provider = s.get(TelehealthProvider, pid)
        if not provider or provider.status != "active":
            return False, "Provider not found or not active"

        if appt.visit_type == "telehealth" and appt.status == "requested":
            avail = provider_availability_label(s, pid)
            if avail != "free":
                return False, f"Provider is {avail} right now — pick another clinician or try later."

        date_iso = (scheduled_date or "").strip()
        if not date_iso:
            date_iso, pref_time = _split_preferred_date_time(appt.preferred_date or "")
            if not scheduled_time or scheduled_time == "09:00":
                scheduled_time = pref_time or scheduled_time
        if not date_iso:
            date_iso = datetime.now(timezone.utc).date().isoformat()
        if not _DATE_RE.match(date_iso):
            return False, "Invalid date — use YYYY-MM-DD"

        time_part = (scheduled_time or "09:00").strip()[:5]
        if len(time_part) == 5 and time_part[2] == ":":
            scheduled_at = f"{date_iso}T{time_part}:00+00:00"
        else:
            scheduled_at = f"{date_iso}T09:00:00+00:00"

        now = now_iso()
        appt.provider_id = pid
        appt.profession = provider.profession
        appt.scheduled_at = scheduled_at
        appt.preferred_date = appt.preferred_date or date_iso
        appt.status = "booked"
        appt.updated_at = now

    sync_appointment_to_ehr(appointment_id)
    _notify_helpdesk_appointment(appointment_id, event="approved")
    if notify_patient:
        ok, detail = notify_patient_appointment_confirmed(appointment_id)
        if not ok:
            return True, f"Assigned and synced to EHR, but patient notify failed: {detail}"
    return True, "Appointment approved, provider assigned, and EHR updated."


def admin_cancel_appointment(appointment_id: str, *, notify_patient: bool = True) -> tuple[bool, str]:
    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return False, "Appointment not found"
        if appt.status == "cancelled":
            return False, "Already cancelled"
        phone = appt.phone_e164
        channel = appt.channel or "whatsapp"
        ref = appointment_ref(appt.id)
        appt.status = "cancelled"
        appt.updated_at = now_iso()

    if notify_patient:
        ch: Literal["sms", "whatsapp"] = "whatsapp" if channel == "whatsapp" else "sms"
        body = f"Your Kareconnect appointment (ref {ref}) has been cancelled. Send APPOINT to book again."
        send_message(to_phone_e164=phone, body=body, channel=ch)
    return True, "Appointment cancelled."


def admin_reschedule_appointment(
    *,
    appointment_id: str,
    new_date: str,
    new_time: Optional[str] = None,
    notify_patient: bool = True,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """
    Move appointment to a new date/time (drag-and-drop). Updates EHR sync.
    Returns (ok, message, updated_event_dict).
    """
    date_iso = (new_date or "").strip()[:10]
    if not _DATE_RE.match(date_iso):
        return False, "Invalid date", None

    with session_scope() as s:
        appt = s.get(Appointment, appointment_id)
        if not appt:
            return False, "Appointment not found", None
        if appt.status == "cancelled":
            return False, "Cannot reschedule a cancelled appointment", None

        time_part = (new_time or appointment_scheduled_time(appt) or "09:00").strip()[:5]
        if len(time_part) != 5 or time_part[2] != ":":
            time_part = "09:00"
        scheduled_at = f"{date_iso}T{time_part}:00+00:00"

        appt.preferred_date = date_iso
        appt.scheduled_at = scheduled_at
        appt.updated_at = now_iso()
        providers: dict[str, TelehealthProvider] = {}
        if appt.provider_id:
            prov = s.get(TelehealthProvider, appt.provider_id)
            if prov:
                providers[prov.id] = prov
        event = _appointment_event_dict(appt, providers)

    sync_appointment_to_ehr(appointment_id)
    msg = "Appointment rescheduled."
    if notify_patient:
        ok, detail = notify_patient_appointment_rescheduled(appointment_id)
        if not ok:
            msg = f"Appointment rescheduled, but patient notify failed: {detail}"
    return True, msg, event


def handle_appointment_message(
    *,
    phone_e164: str,
    channel: Literal["sms", "whatsapp"],
    body: str,
) -> Optional[str]:
    """Returns reply text when handled by appointment flow, else None."""
    import re as _re

    text = (body or "").strip()
    upper = text.upper()
    first = _re.sub(r"[^A-Z0-9]", "", upper.split()[0]) if upper.split() else ""
    phone = normalize_phone_e164(phone_e164)

    with session_scope() as s:
        flow = _get_flow(s, phone, channel)
        if flow.step == "idle":
            return None

        if first in ("CANCEL", "EXIT"):
            _reset_flow(flow)
            return "Appointment booking cancelled."

        if flow.step == "visit_type":
            if text == "1" or first in ("VIDEO", "TELEHEALTH", "TELE"):
                if not list_professions_with_counts():
                    flow.visit_type = "in_person"
                    flow.step = "preferred_date"
                    flow.updated_at = now_iso()
                    return (
                        "No video clinicians are listed. Let's book a clinic visit.\n\n"
                        + _clinic_date_menu_text()
                    )
                flow.visit_type = "telehealth"
                if not (flow.reason or "").strip():
                    flow.step = "reason"
                    flow.updated_at = now_iso()
                    return "Briefly describe the reason for your visit (one short message)."
                flow.step = "profession"
                flow.updated_at = now_iso()
                return _profession_menu_text()
            if text == "2" or first in ("CLINIC", "VISIT", "INPERSON"):
                flow.visit_type = "in_person"
                if not (flow.reason or "").strip():
                    flow.step = "reason"
                    flow.updated_at = now_iso()
                    return "Briefly describe the reason for your clinic visit (one short message)."
                flow.step = "preferred_date"
                flow.updated_at = now_iso()
                return _clinic_date_menu_text()
            return _visit_type_menu(reason_hint=flow.reason)

        if flow.step == "reason":
            if not text:
                return "Please send a short reason for the appointment."
            flow.reason = text[:500]
            flow.updated_at = now_iso()
            if flow.visit_type == "telehealth":
                flow.step = "profession"
                return _profession_menu_text()
            if flow.visit_type == "in_person":
                flow.step = "preferred_date"
                return _clinic_date_menu_text()
            flow.step = "visit_type"
            return _visit_type_menu(reason_hint=flow.reason)

        if flow.step == "profession":
            if upper in ("BACK", "0"):
                flow.step = "visit_type"
                flow.updated_at = now_iso()
                return _visit_type_menu(reason_hint=flow.reason)
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
            if upper in ("BACK", "0"):
                flow.step = "profession"
                flow.profession_code = None
                flow.selected_provider_id = None
                flow.updated_at = now_iso()
                return _profession_menu_text()
            if not text.isdigit():
                return None
            if not flow.profession_code:
                flow.step = "profession"
                return _profession_menu_text()
            providers = list(s.execute(_available_providers_stmt(s, flow.profession_code)).scalars().all())
            idx = int(text)
            if idx < 1 or idx > len(providers):
                busy = len(_busy_provider_ids(s, flow.profession_code))
                hint = f" ({busy} busy — not listed)" if busy else ""
                return f"Reply 1–{len(providers)} to choose an available clinician{hint}, BACK, or CANCEL."
            provider = providers[idx - 1]
            flow.step = "confirm"
            flow.selected_provider_id = provider.id
            flow.updated_at = now_iso()
            return _confirm_text(s, provider, flow.reason or "Video consultation")

        if flow.step == "confirm":
            if upper in ("BACK", "0"):
                flow.step = "provider"
                flow.selected_provider_id = None
                flow.updated_at = now_iso()
                if flow.profession_code:
                    return _provider_menu_text(s, flow.profession_code)
                flow.step = "profession"
                return _profession_menu_text()
            if upper not in ("YES", "Y", "CONFIRM", "OK"):
                return None
            return _book_video_appointment(s, flow=flow, phone_e164=phone, channel=channel)

        if flow.step == "preferred_date":
            if upper in ("BACK", "0"):
                flow.step = "visit_type"
                flow.updated_at = now_iso()
                return _visit_type_menu(reason_hint=flow.reason)

            menu_days = _next_weekdays()
            preferred: Optional[str] = None
            preferred_time: Optional[str] = None
            if text.isdigit():
                idx = int(text)
                if 1 <= idx <= len(menu_days):
                    preferred = menu_days[idx - 1].isoformat()
                else:
                    return (
                        f"Reply 1–{len(menu_days)} to pick a date, send YYYY-MM-DD or "
                        f"YYYY-MM-DD HH:MM, or CANCEL to exit."
                    )
            else:
                parsed = _parse_datetime_preference(text)
                if not parsed:
                    return (
                        "Could not read that date/time.\n\n"
                        + _clinic_date_menu_text()
                    )
                preferred, preferred_time = parsed

            ext_pid: Optional[str] = None
            if flow.user_id:
                from app.models import EhrPatientLink

                link = s.execute(
                    select(EhrPatientLink).where(EhrPatientLink.user_id == flow.user_id)
                ).scalar_one_or_none()
                if link:
                    ext_pid = link.external_patient_id
            slot = fetch_ehr_appointment_availability(
                date_iso=preferred,
                visit_type="in_person",
                external_patient_id=ext_pid,
            )
            if not slot.available:
                return (
                    f"No clinic slots on {preferred}. "
                    f"{slot.message or 'Try another date.'}\n\n"
                    + _clinic_date_menu_text()
                )
            flow.preferred_date = (
                f"{preferred} {preferred_time}" if preferred_time else preferred
            )
            flow.step = "clinic_confirm"
            flow.updated_at = now_iso()
            slot_msg = slot.message
            if slot.slots_remaining:
                slot_msg = f"{slot.slots_remaining} slot(s) reported available. {slot_msg}".strip()
            return _clinic_confirm_text(
                reason=flow.reason or "Clinic visit",
                preferred_date=flow.preferred_date,
                slot_msg=slot_msg,
            )

        if flow.step == "clinic_confirm":
            if upper in ("BACK", "0"):
                flow.step = "preferred_date"
                flow.updated_at = now_iso()
                return _clinic_date_menu_text()
            if upper not in ("YES", "Y", "CONFIRM", "OK"):
                return None
            return _book_clinic_appointment(s, flow=flow, phone_e164=phone, channel=channel)

    return None
