from __future__ import annotations

import html
import logging
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anyio
from fastapi import Request
from sqlalchemy import desc, select
from sqladmin import Admin, BaseView, ModelView, action, expose
from sqladmin.authentication import AuthenticationBackend
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from wtforms import BooleanField, SelectField, StringField, validators
from wtforms.widgets import Input

from app.config import settings
from app.database import engine, session_scope
from markupsafe import Markup

from app.broadcast import (
    BroadcastFilters,
    audience_counts_by_language,
    audience_whatsapp_session_stats,
    count_broadcast_recipients,
    enqueue_broadcast,
    deliver_broadcast_run,
    get_broadcast_run_detail,
    list_recent_broadcast_runs,
    preview_broadcast_recipients,
)
from app.models import (
    AdminUser,
    AppSettings,
    Appointment,
    BroadcastRun,
    Enrollment,
    Escalation,
    HelpdeskTicket,
    InboundMessage,
    IntegrationSettings,
    MessageOutbox,
    ClinicalReminder,
    EhrPatientLink,
    EhrSyncLog,
    GlucoseReading,
    MedicationAdjustmentDraft,
    PatientMediaAttachment,
    PatientNextOfKin,
    ProviderRating,
    RpmEnrollment,
    TelehealthProvider,
    TelehealthSession,
    TriageAssessment,
    TriageSession,
    TranslationCache,
    TranslationLearningSample,
    VoiceMessageLog,
    TwilioDeliveryEvent,
    User,
)
from app.telehealth import (
    format_fee,
    mark_session_call_ended,
    mark_session_call_started,
    mask_phone_for_display,
    profession_label,
    provider_availability_label,
    reassign_telehealth_provider,
    resend_patient_telehealth_message,
    resend_provider_telehealth_notification,
    resend_telehealth_confirmations,
    send_post_call_feedback_prompts,
)
from app.appointments import (
    admin_approve_and_assign_appointment,
    admin_cancel_appointment,
    admin_reschedule_appointment,
    appointment_ref,
    get_appointment_assign_context,
    list_appointment_events,
    list_pending_appointments,
    load_calendar_page_context,
)
from app import form_choices as fc
from app.integration_self_test import (
    format_outcome_html,
    safe_back_url,
    test_africastalking,
    test_broadcast_template,
    test_meta_whatsapp,
    test_paystack,
    test_twilio,
)
from app.translation_monitoring import gather_translation_monitoring, run_live_pcm_smoke
from app.webhook_self_check import (
    test_meta_webhook_verify,
    test_twilio_inbound_webhook,
    test_twilio_status_webhook,
)
from app.security import verify_password
from app.util import format_datetime_human, install_jinja_datetime_filters, new_id, now_iso


class _DateTimeLocalInput(Input):
    input_type = "datetime-local"


class IsoDateTimeLocalField(StringField):
    """DB stores ISO UTC strings; HTML uses datetime-local (minute precision)."""

    widget = _DateTimeLocalInput()

    def process_formdata(self, valuelist: list[str]) -> None:
        if valuelist and valuelist[0] and str(valuelist[0]).strip():
            raw = str(valuelist[0]).strip()
            if len(raw) >= 16 and raw[4] == "-" and raw[7] == "-" and "T" in raw[:11]:
                slot = raw[:16]
                dt = datetime.strptime(slot, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            self.data = dt.replace(microsecond=0).isoformat()
        else:
            self.data = None

    def process_data(self, value: object) -> None:
        if value is None or value == "":
            self.data = ""
            return
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            self.data = dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M")
            return
        s = str(value).strip().replace("Z", "").replace("+00:00", "")
        if "T" in s:
            date_part, _, time_part = s.partition("T")
            hm = time_part[:5] if len(time_part) >= 5 else "00:00"
            self.data = f"{date_part}T{hm}"
        else:
            self.data = (s[:10] + "T00:00") if len(s) >= 10 else ""


def _enrollment_user_cell(model: Enrollment, attr: Any) -> str:
    u = getattr(model, "user", None)
    if u is None:
        return model.user_id
    nm = (u.full_name or "").strip()
    ph = (u.phone_e164 or "").strip()
    if nm and ph:
        return f"{nm} · {ph}"
    return nm or ph or model.user_id


def _truncate_list_cell(field: str, width: int = 56):
    """Keep sqladmin list rows readable without horizontal scroll."""

    def _inner(model: Any, attr: Any) -> str:
        v = getattr(model, field, None)
        s = ("" if v is None else str(v)).strip()
        if not s:
            return "—"
        return s if len(s) <= width else s[: width - 1] + "…"

    return _inner


def _verify_admin_credentials(username: str, password: str) -> bool:
    if not username or not password:
        return False
    uname = username.strip()
    with session_scope() as s:
        row = s.execute(select(AdminUser).where(AdminUser.username == uname)).scalar_one_or_none()
        if row is None or not row.is_active:
            return False
        return verify_password(password, row.password_hash)


class SimpleAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        ok = await anyio.to_thread.run_sync(_verify_admin_credentials, username, password)
        if ok:
            request.session.update({"hc_admin": "1", "hc_admin_user": username.strip()})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return request.session.get("hc_admin") == "1"


def _merge_datetime_column_formatters(view_cls) -> None:
    from app.util import format_date_human

    cols = getattr(view_cls, "column_list", None) or []
    if not cols:
        return
    existing = dict(getattr(view_cls, "column_formatters", None) or {})
    for col in cols:
        if col in existing:
            continue
        key = getattr(col, "key", None)
        if not key:
            continue
        if key.endswith("_at") or key in ("due_at", "expires_at"):
            existing[col] = lambda m, a, k=key: format_datetime_human(getattr(m, k, None))
        elif key.endswith("_date") or key in ("date", "preferred_date"):
            existing[col] = lambda m, a, k=key: format_date_human(getattr(m, k, None))
    view_cls.column_formatters = existing


class HCModelView(ModelView):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _merge_datetime_column_formatters(cls)


class TwilioDeliveryEventAdmin(HCModelView, model=TwilioDeliveryEvent):
    column_list = [
        TwilioDeliveryEvent.id,
        TwilioDeliveryEvent.message_sid,
        TwilioDeliveryEvent.message_status,
        TwilioDeliveryEvent.error_code,
        TwilioDeliveryEvent.to_value,
        TwilioDeliveryEvent.from_value,
        TwilioDeliveryEvent.created_at,
    ]
    column_sortable_list = [TwilioDeliveryEvent.created_at]
    column_default_sort = [(TwilioDeliveryEvent.created_at, True)]
    can_create = False
    can_edit = False
    can_delete = False
    name = "Twilio delivery event"
    name_plural = "Twilio delivery events"


class AdminUserAdmin(HCModelView, model=AdminUser):
    column_list = [AdminUser.id, AdminUser.username, AdminUser.is_active, AdminUser.is_superuser, AdminUser.created_at, AdminUser.updated_at]
    column_searchable_list = [AdminUser.username]
    column_sortable_list = [AdminUser.created_at]
    form_columns = [AdminUser.is_active, AdminUser.is_superuser]
    can_create = False
    can_delete = False
    name = "Admin user"
    name_plural = "Admin users"


class UserAdmin(HCModelView, model=User):
    column_list = [
        User.id,
        User.full_name,
        User.phone_e164,
        User.channel,
        User.language,
        User.recurring_messages_opt_in,
        User.created_at,
        User.updated_at,
    ]
    column_searchable_list = [User.phone_e164, User.full_name]
    column_sortable_list = [User.created_at, User.updated_at, User.full_name]
    form_columns = [
        User.phone_e164,
        User.full_name,
        User.channel,
        User.language,
        User.recurring_messages_opt_in,
    ]
    form_overrides = {
        "channel": SelectField,
        "language": SelectField,
        "recurring_messages_opt_in": BooleanField,
    }
    form_args = {
        "channel": {"choices": fc.CHANNEL_CHOICES, "coerce": str},
        "language": {"choices": fc.LANGUAGE_CHOICES, "coerce": str},
    }
    name = "User"
    name_plural = "Users"


class EnrollmentAdmin(HCModelView, model=Enrollment):
    column_list = [
        Enrollment.id,
        Enrollment.user,
        Enrollment.program,
        Enrollment.status,
        Enrollment.edd,
        Enrollment.created_at,
        Enrollment.updated_at,
    ]
    column_formatters = {Enrollment.user: _enrollment_user_cell}
    column_labels = {Enrollment.user: "Participant (name · phone)"}
    # Do not override list_query with joinedload(user): sqladmin already applies
    # selectinload for relationship columns; mixing loaders causes 500s on list.

    column_searchable_list = [Enrollment.user_id, Enrollment.program]
    column_sortable_list = [Enrollment.created_at, Enrollment.updated_at]
    form_columns = [Enrollment.user_id, Enrollment.program, Enrollment.status, Enrollment.edd]
    form_overrides = {"program": SelectField, "status": SelectField}
    form_args = {
        "program": {"choices": fc.PROGRAM_CHOICES, "coerce": str},
        "status": {"choices": fc.ENROLLMENT_STATUS_CHOICES, "coerce": str},
    }
    name = "Enrollment"
    name_plural = "Enrollments"


class MessageOutboxAdmin(HCModelView, model=MessageOutbox):
    column_list = [
        MessageOutbox.id,
        MessageOutbox.user_id,
        MessageOutbox.program,
        MessageOutbox.channel,
        MessageOutbox.language,
        MessageOutbox.to_phone_e164,
        MessageOutbox.status,
        MessageOutbox.due_at,
        MessageOutbox.sent_at,
    ]
    column_searchable_list = [MessageOutbox.to_phone_e164, MessageOutbox.program, MessageOutbox.status]
    column_sortable_list = [MessageOutbox.due_at, MessageOutbox.sent_at]
    form_columns = [
        MessageOutbox.user_id,
        MessageOutbox.program,
        MessageOutbox.channel,
        MessageOutbox.language,
        MessageOutbox.to_phone_e164,
        MessageOutbox.body,
        MessageOutbox.due_at,
        MessageOutbox.created_at,
        MessageOutbox.status,
        MessageOutbox.error,
    ]
    form_overrides = {
        "program": SelectField,
        "channel": SelectField,
        "language": SelectField,
        "status": SelectField,
        "due_at": IsoDateTimeLocalField,
        "created_at": IsoDateTimeLocalField,
    }
    form_args = {
        "program": {"choices": fc.PROGRAM_CHOICES, "coerce": str},
        "channel": {"choices": fc.CHANNEL_CHOICES, "coerce": str},
        "language": {"choices": fc.LANGUAGE_CHOICES, "coerce": str},
        "status": {"choices": fc.OUTBOX_STATUS_CHOICES, "coerce": str},
        "due_at": {"validators": [validators.Optional()]},
        "created_at": {"validators": [validators.Optional()]},
    }
    column_labels = {
        MessageOutbox.due_at: "Due at (UTC, calendar)",
        MessageOutbox.created_at: "Created at (UTC, calendar)",
    }
    name = "Outbox Message"
    name_plural = "Outbox"

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        if is_created and not data.get("id"):
            data["id"] = new_id("msg")
        created = data.get("created_at")
        if is_created and (created is None or (isinstance(created, str) and not created.strip())):
            data["created_at"] = now_iso()
        due = data.get("due_at")
        if is_created and (due is None or (isinstance(due, str) and not due.strip())):
            data["due_at"] = now_iso()
        if data.get("status") == "sent":
            prior = None if is_created else getattr(model, "sent_at", None)
            if not prior:
                data["sent_at"] = now_iso()
        else:
            data["sent_at"] = None


class InboundMessageAdmin(HCModelView, model=InboundMessage):
    column_list = [InboundMessage.id, InboundMessage.channel, InboundMessage.from_phone_e164, InboundMessage.body, InboundMessage.created_at]
    column_searchable_list = [InboundMessage.from_phone_e164, InboundMessage.body]
    column_sortable_list = [InboundMessage.created_at]
    form_overrides = {"channel": SelectField}
    form_args = {"channel": {"choices": fc.CHANNEL_CHOICES, "coerce": str}}
    name = "Inbound Message"
    name_plural = "Inbound"


class HelpdeskTicketAdmin(HCModelView, model=HelpdeskTicket):
    column_list = [
        HelpdeskTicket.id,
        HelpdeskTicket.from_phone_e164,
        HelpdeskTicket.channel,
        HelpdeskTicket.question,
        HelpdeskTicket.suggested_answer,
        HelpdeskTicket.ai_symptom_reply,
        HelpdeskTicket.status,
        HelpdeskTicket.created_at,
        HelpdeskTicket.updated_at,
    ]
    column_formatters = {
        HelpdeskTicket.ai_symptom_reply: _truncate_list_cell("ai_symptom_reply", 56),
    }
    column_searchable_list = [HelpdeskTicket.from_phone_e164, HelpdeskTicket.question, HelpdeskTicket.status]
    column_sortable_list = [HelpdeskTicket.created_at, HelpdeskTicket.updated_at]
    form_columns = [
        HelpdeskTicket.from_phone_e164,
        HelpdeskTicket.channel,
        HelpdeskTicket.question,
        HelpdeskTicket.suggested_answer,
        HelpdeskTicket.ai_symptom_reply,
        HelpdeskTicket.status,
    ]
    form_overrides = {"channel": SelectField, "status": SelectField}
    form_args = {
        "channel": {"choices": fc.CHANNEL_CHOICES, "coerce": str},
        "status": {"choices": fc.HELPDESK_STATUS_CHOICES, "coerce": str},
    }
    column_labels = {
        HelpdeskTicket.suggested_answer: "FAQ suggestion (auto)",
        HelpdeskTicket.ai_symptom_reply: "KareVault AI / symptom checker (informational only)",
        HelpdeskTicket.question: "Patient message",
    }
    name = "Helpdesk Ticket"
    name_plural = "Helpdesk"


class AppointmentAdmin(HCModelView, model=Appointment):
    category = "Operations"
    column_list = [
        Appointment.id,
        Appointment.phone_e164,
        Appointment.visit_type,
        Appointment.reason,
        Appointment.preferred_date,
        Appointment.scheduled_at,
        Appointment.provider_id,
        Appointment.status,
        Appointment.ehr_sync_status,
        Appointment.telehealth_session_id,
        Appointment.created_at,
    ]
    column_searchable_list = [
        Appointment.phone_e164,
        Appointment.reason,
        Appointment.status,
        Appointment.visit_type,
        Appointment.ehr_appointment_id,
    ]
    form_columns = [
        Appointment.phone_e164,
        Appointment.visit_type,
        Appointment.reason,
        Appointment.preferred_date,
        Appointment.scheduled_at,
        Appointment.profession,
        Appointment.provider_id,
        Appointment.status,
        Appointment.channel,
        Appointment.telehealth_session_id,
        Appointment.ehr_appointment_id,
        Appointment.ehr_sync_status,
    ]
    form_overrides = {"status": SelectField, "visit_type": SelectField, "ehr_sync_status": SelectField}
    form_args = {
        "status": {"choices": fc.APPOINTMENT_STATUS_CHOICES, "coerce": str},
        "visit_type": {"choices": fc.APPOINTMENT_VISIT_TYPE_CHOICES, "coerce": str},
        "ehr_sync_status": {"choices": fc.APPOINTMENT_EHR_SYNC_CHOICES, "coerce": str},
    }
    column_formatters = {
        Appointment.id: lambda m, _a: appointment_ref(m.id),
        Appointment.visit_type: lambda m, _a: "Video" if m.visit_type == "telehealth" else "Clinic",
        Appointment.scheduled_at: lambda m, _a: _utc_short(m.scheduled_at),
    }
    name = "Appointment records"
    name_plural = "Appointment records"

    @action(
        name="approve_assign",
        label="Approve & assign",
        confirmation_message="Open assign form for selected appointment?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def approve_assign_action(self, request: Request) -> RedirectResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        if not pks:
            raise HTTPException(status_code=400, detail="No appointment selected")
        return RedirectResponse(url=f"/admin/appointment/assign?pk={pks[0]}", status_code=302)

    @action(
        name="cancel_appointment",
        label="Cancel",
        confirmation_message="Cancel selected appointment(s) and notify patient?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def cancel_appointment_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(lambda aid=pk: admin_cancel_appointment(aid))
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        body = "<br/>".join(html.escape(ln) for ln in lines)
        back = (request.headers.get("referer") or "/admin/appointment/list").strip()
        return HTMLResponse(
            f'<html><body><h2>Cancel appointment</h2><p>{body}</p>'
            f'<p><a href="{html.escape(back)}">Back</a></p></body></html>'
        )


class AppointmentCalendarView(BaseView):
    """Day / week / month calendar with drag-and-drop reschedule."""

    name = "Appointments"
    icon = "fa-solid fa-calendar-days"
    category = "Operations"
    identity = "appointment"

    def _parse_anchor(self, request: Request) -> tuple[str, date]:
        view = (request.query_params.get("view") or "month").strip().lower()
        if view not in ("day", "week", "month"):
            view = "month"
        raw = (request.query_params.get("date") or "").strip()
        if raw:
            try:
                return view, date.fromisoformat(raw[:10])
            except ValueError:
                pass
        # Legacy year/month query params
        now = datetime.now(timezone.utc).date()
        try:
            year = int(request.query_params.get("year") or now.year)
            month = int(request.query_params.get("month") or now.month)
            if 1 <= month <= 12:
                return view, date(year, month, min(now.day, 28))
        except (TypeError, ValueError):
            pass
        return view, now

    @expose("/appointment", methods=["GET"], identity="appointment")
    async def appointment_calendar(self, request: Request):
        view, anchor = self._parse_anchor(request)
        page = await anyio.to_thread.run_sync(
            lambda: load_calendar_page_context(view=view, anchor=anchor)
        )
        flash = None
        if request.query_params.get("assigned") == "1":
            flash = {"level": "success", "message": "Appointment approved and provider assigned."}
        if request.query_params.get("rescheduled") == "1":
            flash = {"level": "success", "message": "Appointment rescheduled."}
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/appointment_calendar.html",
            {
                "page": page,
                "page_json": json.dumps(page),
                "today": datetime.now(timezone.utc).date().isoformat(),
                "flash": flash,
            },
        )

    @expose("/appointment/events", methods=["GET"], identity="appointment-events")
    async def appointment_events(self, request: Request):
        start_raw = (request.query_params.get("start") or "").strip()[:10]
        end_raw = (request.query_params.get("end") or "").strip()[:10]
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="start and end dates required (YYYY-MM-DD)")
        if end < start:
            start, end = end, start
        events = await anyio.to_thread.run_sync(
            lambda: list_appointment_events(start=start, end=end)
        )
        pending = await anyio.to_thread.run_sync(list_pending_appointments)
        return JSONResponse({"events": events, "pending": pending})

    @expose("/appointment/reschedule", methods=["POST"], identity="appointment-reschedule")
    async def appointment_reschedule(self, request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        appointment_id = (body.get("appointment_id") or body.get("id") or "").strip()
        new_date = (body.get("date") or body.get("new_date") or "").strip()[:10]
        new_time = (body.get("time") or body.get("new_time") or "").strip() or None
        notify = body.get("notify_patient", True) not in (False, 0, "0", "false", "no", "off")
        if not appointment_id or not new_date:
            raise HTTPException(status_code=400, detail="appointment_id and date are required")

        ok, msg, event = await anyio.to_thread.run_sync(
            lambda: admin_reschedule_appointment(
                appointment_id=appointment_id,
                new_date=new_date,
                new_time=new_time,
                notify_patient=notify,
            )
        )
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return JSONResponse({"ok": True, "message": msg, "event": event})

    @expose("/appointment/assign", methods=["GET", "POST"], identity="appointment-assign")
    async def appointment_assign(self, request: Request):
        pk = (request.query_params.get("pk") or "").strip()
        if not pk:
            raise HTTPException(status_code=400, detail="Missing appointment id (pk)")

        if request.method == "GET":
            ctx = await anyio.to_thread.run_sync(lambda: get_appointment_assign_context(pk))
            if not ctx:
                raise HTTPException(status_code=404, detail="Appointment not found")
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/appointment_assign.html",
                {"ctx": ctx, "error": None, "success": None},
            )

        form = await request.form()
        provider_id = (form.get("provider_id") or "").strip()
        scheduled_date = (form.get("scheduled_date") or "").strip() or None
        scheduled_time = (form.get("scheduled_time") or "09:00").strip()
        notify_patient = form.get("notify_patient") in ("1", "on", "true", "yes")
        ok, msg = await anyio.to_thread.run_sync(
            lambda: admin_approve_and_assign_appointment(
                appointment_id=pk,
                provider_id=provider_id,
                scheduled_date=scheduled_date,
                scheduled_time=scheduled_time,
                notify_patient=notify_patient,
            )
        )
        if ok:
            return RedirectResponse(url="/admin/appointment?assigned=1", status_code=302)
        ctx = await anyio.to_thread.run_sync(lambda: get_appointment_assign_context(pk))
        if not ctx:
            raise HTTPException(status_code=404, detail="Appointment not found")
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/appointment_assign.html",
            {"ctx": ctx, "error": msg, "success": None},
            status_code=400,
        )


class EscalationAdmin(HCModelView, model=Escalation):
    column_list = [Escalation.id, Escalation.phone_e164, Escalation.urgency, Escalation.status, Escalation.created_at]
    column_searchable_list = [Escalation.phone_e164, Escalation.reason, Escalation.status, Escalation.urgency]
    form_overrides = {"urgency": SelectField, "status": SelectField}
    form_args = {
        "urgency": {"choices": fc.ESCALATION_URGENCY_CHOICES, "coerce": str},
        "status": {"choices": fc.ESCALATION_STATUS_CHOICES, "coerce": str},
    }
    name = "Escalation"
    name_plural = "Telehealth Escalations"


class RpmEnrollmentAdmin(HCModelView, model=RpmEnrollment):
    column_list = [RpmEnrollment.id, RpmEnrollment.phone_e164, RpmEnrollment.condition, RpmEnrollment.status, RpmEnrollment.created_at]
    column_searchable_list = [RpmEnrollment.phone_e164, RpmEnrollment.condition, RpmEnrollment.status]
    form_overrides = {"status": SelectField}
    form_args = {"status": {"choices": fc.RPM_STATUS_CHOICES, "coerce": str}}
    name = "RPM Enrollment"
    name_plural = "RPM"


def _fee_cell(model: TelehealthProvider, _attr: Any) -> str:
    return format_fee(model.fee_ngn, model.currency)


def _profession_cell(model: TelehealthProvider, _attr: Any) -> str:
    return profession_label(model.profession)


def _masked_patient_cell(model: TelehealthSession, _attr: Any) -> str:
    if model.mask_patient_number:
        return mask_phone_for_display(model.patient_phone_e164, session_id=model.id)
    return model.patient_phone_e164 or "—"


def _telehealth_ref_cell(model: TelehealthSession, _attr: Any) -> str:
    return (model.id or "").replace("ths_", "TH-")[:16].upper() or "—"


def _telehealth_live_status_cell(model: TelehealthSession, _attr: Any) -> Markup:
    status = (model.status or "").lower()
    call = (model.call_status or "").lower()
    joined = bool(model.provider_joined)
    badges: list[tuple[str, str]] = []
    if status == "confirmed":
        badges.append(("azure", "Booked"))
    elif status == "pending_payment":
        badges.append(("yellow", "Awaiting payment"))
    elif status == "in_progress":
        if call == "connected":
            badges.append(("green", "On call"))
        elif call in ("ringing", "initiated"):
            badges.append(("cyan", "Calling"))
        elif call == "ended":
            badges.append(("yellow", "Ended — awaiting rating"))
        else:
            badges.append(("cyan", "In progress"))
    elif status == "completed":
        badges.append(("green", "Completed"))
    elif status == "no_show":
        badges.append(("red", "No show"))
    elif status == "cancelled":
        badges.append(("secondary", "Cancelled"))
    if joined:
        badges.append(("green", "Provider joined"))
    elif call == "ended" and not joined:
        badges.append(("red", "Provider not on call"))
    with session_scope() as s:
        score = s.execute(
            select(ProviderRating.score).where(ProviderRating.session_id == model.id).limit(1)
        ).scalar_one_or_none()
    if score is not None:
        badges.append(("green", f"Rated {int(score)}/5"))
    elif call == "ended":
        badges.append(("yellow", "Awaiting RATE"))
    if not badges:
        return Markup("—")
    return Markup(" ".join(f'<span class="badge bg-{color}-lt">{label}</span>' for color, label in badges))


def _utc_short(ts: Optional[str]) -> str:
    return format_datetime_human(ts)


def _provider_availability_cell(model: TelehealthProvider, _attr: Any) -> Markup:
    with session_scope() as s:
        label = provider_availability_label(s, model.id)
    colors = {"free": "green", "booked": "yellow", "in_session": "red"}
    text = {"free": "Free", "booked": "Booked", "in_session": "In session"}.get(label, label)
    return Markup(f'<span class="badge bg-{colors.get(label, "secondary")}-lt">{text}</span>')


class TelehealthProviderAdmin(HCModelView, model=TelehealthProvider):
    category = "Telehealth"
    column_list = [
        TelehealthProvider.full_name,
        TelehealthProvider.title,
        TelehealthProvider.profession,
        TelehealthProvider.fee_ngn,
        TelehealthProvider.status,
        TelehealthProvider.external_doctor_id,
        TelehealthProvider.id,
        TelehealthProvider.avg_rating,
        TelehealthProvider.rating_count,
        TelehealthProvider.updated_at,
    ]
    column_searchable_list = [
        TelehealthProvider.full_name,
        TelehealthProvider.profession,
        TelehealthProvider.license_number,
        TelehealthProvider.status,
    ]
    column_sortable_list = [
        TelehealthProvider.full_name,
        TelehealthProvider.fee_ngn,
        TelehealthProvider.avg_rating,
        TelehealthProvider.updated_at,
    ]
    column_formatters = {
        TelehealthProvider.fee_ngn: _fee_cell,
        TelehealthProvider.profession: _profession_cell,
        TelehealthProvider.id: _provider_availability_cell,
    }
    form_columns = [
        TelehealthProvider.full_name,
        TelehealthProvider.title,
        TelehealthProvider.profession,
        TelehealthProvider.bio,
        TelehealthProvider.license_number,
        TelehealthProvider.contact_phone_e164,
        TelehealthProvider.fee_ngn,
        TelehealthProvider.currency,
        TelehealthProvider.status,
        TelehealthProvider.mask_patient_number,
    ]
    form_overrides = {
        "status": SelectField,
        "mask_patient_number": BooleanField,
    }
    form_args = {
        # Not a strict SelectField: rows synced from the KareVault Telehealth API carry the
        # vendor's own free-text specialty (e.g. "Cardiologist", "Dentist"), which doesn't fit
        # the fixed local list — a SelectField would silently reset it to the first choice on
        # save. Free text still works fine for locally hand-entered providers.
        "status": {"choices": fc.TELEHEALTH_PROVIDER_STATUS_CHOICES, "coerce": str, "default": "active"},
        "fee_ngn": {"validators": [validators.NumberRange(min=0)], "default": 0},
    }
    column_labels = {
        TelehealthProvider.fee_ngn: "Consultation fee (NGN)",
        TelehealthProvider.id: "Availability (live)",
        TelehealthProvider.external_doctor_id: "KareVault doctor id (set only for synced rows)",
        TelehealthProvider.contact_phone_e164: "Provider WhatsApp / phone (E.164) — booking + payment alerts",
        TelehealthProvider.mask_patient_number: "Mask patient number in admin session views",
        TelehealthProvider.status: "Listing status (only Active appears to patients). Rows added here "
        "manually are only shown to patients when the KareVault Telehealth API is not configured.",
    }
    name = "Provider"
    name_plural = "Providers"

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        if is_created and not data.get("id"):
            data["id"] = new_id("thp")
        now = now_iso()
        if is_created:
            data["created_at"] = now
            if not (data.get("status") or "").strip():
                data["status"] = "active"
        data["updated_at"] = now
        if data.get("fee_ngn") is not None:
            data["fee_ngn"] = max(0, int(data["fee_ngn"]))


class TelehealthSessionAdmin(HCModelView, model=TelehealthSession):
    category = "Telehealth"
    list_template = "sqladmin/telehealth_session_list.html"
    details_template = "sqladmin/telehealth_session_details.html"
    column_list = [
        TelehealthSession.id,
        TelehealthSession.patient_phone_e164,
        TelehealthSession.provider_id,
        TelehealthSession.billing_status,
        TelehealthSession.status,
        TelehealthSession.provider_joined,
        TelehealthSession.call_started_at,
        TelehealthSession.call_ended_at,
        TelehealthSession.payment_instructions_sent_at,
        TelehealthSession.channel,
        TelehealthSession.created_at,
    ]
    column_searchable_list = [
        TelehealthSession.id,
        TelehealthSession.patient_phone_e164,
        TelehealthSession.billing_reference,
        TelehealthSession.status,
        TelehealthSession.billing_status,
    ]
    column_sortable_list = [TelehealthSession.created_at, TelehealthSession.fee_ngn]
    column_formatters = {
        TelehealthSession.id: _telehealth_ref_cell,
        TelehealthSession.patient_phone_e164: _masked_patient_cell,
        TelehealthSession.fee_ngn: lambda m, _a: format_fee(m.fee_ngn, m.currency),
        TelehealthSession.profession_snapshot: lambda m, _a: profession_label(m.profession_snapshot),
        TelehealthSession.status: _telehealth_live_status_cell,
        TelehealthSession.provider_joined: lambda m, _a: "Yes" if m.provider_joined else "No",
        TelehealthSession.call_started_at: lambda m, _a: _utc_short(m.call_started_at),
        TelehealthSession.call_ended_at: lambda m, _a: _utc_short(m.call_ended_at),
        TelehealthSession.payment_instructions_sent_at: lambda m, _a: _utc_short(m.payment_instructions_sent_at),
        TelehealthSession.billing_status: lambda m, _a: (m.billing_status or "—").replace("_", " ").title(),
    }
    form_columns = [
        TelehealthSession.patient_phone_e164,
        TelehealthSession.provider_id,
        TelehealthSession.channel,
        TelehealthSession.profession_snapshot,
        TelehealthSession.status,
        TelehealthSession.fee_ngn,
        TelehealthSession.currency,
        TelehealthSession.billing_status,
        TelehealthSession.billing_reference,
        TelehealthSession.billing_notes,
        TelehealthSession.payment_url,
        TelehealthSession.call_mode,
        TelehealthSession.call_id,
        TelehealthSession.call_status,
        TelehealthSession.call_started_at,
        TelehealthSession.call_ended_at,
        TelehealthSession.provider_joined,
        TelehealthSession.feedback_prompt_sent_at,
        TelehealthSession.call_note,
        TelehealthSession.mask_patient_number,
        TelehealthSession.completed_at,
    ]
    form_overrides = {
        "channel": SelectField,
        "status": SelectField,
        "billing_status": SelectField,
        "call_mode": SelectField,
        "mask_patient_number": BooleanField,
        "completed_at": IsoDateTimeLocalField,
    }
    form_args = {
        "channel": {"choices": [("sms", "SMS"), ("whatsapp", "WhatsApp")], "coerce": str},
        "status": {"choices": fc.TELEHEALTH_SESSION_STATUS_CHOICES, "coerce": str},
        "billing_status": {"choices": fc.TELEHEALTH_BILLING_STATUS_CHOICES, "coerce": str},
        "call_mode": {"choices": fc.TELEHEALTH_CALL_MODE_CHOICES, "coerce": str},
        "completed_at": {"validators": [validators.Optional()]},
    }
    column_labels = {
        TelehealthSession.patient_phone_e164: "Patient (masked when enabled)",
        TelehealthSession.fee_ngn: "Fee at booking (NGN)",
        TelehealthSession.billing_reference: "Payment reference (bank transfer, Paystack, etc.)",
        TelehealthSession.billing_notes: "Billing notes for finance team",
        TelehealthSession.status: "Live session status (updates from WhatsApp call webhooks or admin actions)",
        TelehealthSession.provider_joined: "True when provider connected to the WhatsApp video call",
        TelehealthSession.call_started_at: "When the call connected (UTC)",
        TelehealthSession.call_ended_at: "When the call ended (UTC)",
        TelehealthSession.feedback_prompt_sent_at: "When post-call RATE prompts were sent",
    }
    name = "Session"
    name_plural = "Sessions"

    @staticmethod
    def _telehealth_back_url(referer: Optional[str]) -> str:
        if referer and "/admin/" in referer:
            return referer
        return "/admin/telehealth-session/list"

    @staticmethod
    def _action_result_html(title: str, lines: list[str], back_url: str) -> HTMLResponse:
        body = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"/><title>{html.escape(title)}</title></head>
<body style="font-family:system-ui;max-width:42rem;margin:2rem auto;line-height:1.5">
<h1>{html.escape(title)}</h1>
<ul>{body}</ul>
<p style="margin-top:2rem"><a href="{html.escape(back_url)}">← Back to sessions</a></p>
</body></html>"""
        )

    async def _run_telehealth_action(
        self,
        request: Request,
        *,
        title: str,
        pks: list[str],
        fn,
    ) -> HTMLResponse:
        lines: list[str] = []
        for pk in pks:
            try:
                ok, msg = await anyio.to_thread.run_sync(lambda sid=pk: fn(sid))
                lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
            except Exception as exc:
                logging.getLogger(__name__).exception("telehealth admin action %s failed pk=%s", title, pk)
                lines.append(f"{pk} ERROR: {exc}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html(title, lines or ["No session selected."], back)

    @action(
        name="resend_confirmations",
        label="Resend all",
        confirmation_message="Resend booking/payment messages to patient, provider, and helpdesk?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def resend_confirmations_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            if len(pks) > 1:
                lines.append(f"Session {pk}:")
            lines.extend(await anyio.to_thread.run_sync(lambda sid=pk: resend_telehealth_confirmations(sid)))
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Resend confirmations", lines or ["No session selected."], back)

    @action(
        name="resend_patient",
        label="Resend patient",
        confirmation_message="Resend payment or call instructions to the patient?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def resend_patient_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        return await self._run_telehealth_action(
            request,
            title="Resend patient message",
            pks=pks,
            fn=lambda sid: resend_patient_telehealth_message(sid, notify_provider=False),
        )

    @action(
        name="resend_provider",
        label="Resend provider",
        confirmation_message="Resend booking/payment alert to the assigned provider?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def resend_provider_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(lambda sid=pk: resend_provider_telehealth_notification(sid))
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Resend provider alert", lines or ["No session selected."], back)

    @action(
        name="send_feedback_prompts",
        label="Send feedback prompts",
        confirmation_message="Send post-call RATE prompt to patient and summary to provider?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def send_feedback_prompts_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(lambda sid=pk: send_post_call_feedback_prompts(sid, force=True))
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Send feedback prompts", lines or ["No session selected."], back)

    @action(
        name="mark_call_started",
        label="Mark call started",
        confirmation_message="Mark this session as in progress with provider joined?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def mark_call_started_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(lambda sid=pk: mark_session_call_started(sid))
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Mark call started", lines or ["No session selected."], back)

    @action(
        name="mark_call_ended_joined",
        label="Mark call ended (joined)",
        confirmation_message="Mark call ended with provider on the call and send feedback prompts?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def mark_call_ended_joined_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(
                lambda sid=pk: mark_session_call_ended(sid, provider_joined=True, send_prompts=True)
            )
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Mark call ended (joined)", lines or ["No session selected."], back)

    @action(
        name="mark_no_show",
        label="Mark no show",
        confirmation_message="Mark as no show (provider did not join) and notify parties?",
        add_in_list=True,
        add_in_detail=True,
    )
    async def mark_no_show_action(self, request: Request) -> HTMLResponse:
        pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
        lines: list[str] = []
        for pk in pks:
            ok, msg = await anyio.to_thread.run_sync(
                lambda sid=pk: mark_session_call_ended(sid, provider_joined=False, send_prompts=True)
            )
            lines.append(f"{pk}: {msg}" if ok else f"{pk} FAILED: {msg}")
        back = self._telehealth_back_url(request.headers.get("referer"))
        return self._action_result_html("Mark no show", lines or ["No session selected."], back)

    @expose("/reassign", methods=["GET", "POST"])
    async def reassign_provider_page(self, request: Request) -> HTMLResponse | RedirectResponse:
        pk = (request.query_params.get("pk") or "").strip()
        if not pk:
            raise HTTPException(status_code=400, detail="Missing session id (pk)")

        if request.method == "GET":
            with session_scope() as s:
                session = s.get(TelehealthSession, pk)
                if not session:
                    raise HTTPException(status_code=404, detail="Session not found")
                current = s.get(TelehealthProvider, session.provider_id)
                providers = (
                    s.execute(
                        select(TelehealthProvider)
                        .where(TelehealthProvider.status == "active")
                        .order_by(TelehealthProvider.full_name.asc())
                    )
                    .scalars()
                    .all()
                )
                ctx = {
                    "session_id": session.id,
                    "session_ref": session.id.replace("ths_", "TH-")[:16].upper(),
                    "patient_phone": session.patient_phone_e164,
                    "billing_status": session.billing_status,
                    "current_provider_id": session.provider_id,
                    "current_provider_name": (
                        f"{current.title or ''} {current.full_name or ''}".strip() if current else "—"
                    ),
                    "providers": [
                        {
                            "id": p.id,
                            "label": f"{p.title or ''} {p.full_name} — {profession_label(p.profession)} ({format_fee(p.fee_ngn, p.currency)})".strip(),
                        }
                        for p in providers
                    ],
                }
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/telehealth_session_reassign.html",
                ctx,
            )

        form = await request.form()
        new_provider_id = (form.get("provider_id") or "").strip()
        ok, msg = await anyio.to_thread.run_sync(lambda: reassign_telehealth_provider(pk, new_provider_id))
        if ok:
            return RedirectResponse(
                url=f"/admin/telehealth-session/details/{pk}?reassigned=1",
                status_code=302,
            )
        with session_scope() as s:
            session = s.get(TelehealthSession, pk)
            current = s.get(TelehealthProvider, session.provider_id) if session else None
            providers = (
                s.execute(
                    select(TelehealthProvider)
                    .where(TelehealthProvider.status == "active")
                    .order_by(TelehealthProvider.full_name.asc())
                )
                .scalars()
                .all()
            )
            ctx = {
                "session_id": pk,
                "session_ref": pk.replace("ths_", "TH-")[:16].upper(),
                "patient_phone": session.patient_phone_e164 if session else "",
                "billing_status": session.billing_status if session else "",
                "current_provider_id": session.provider_id if session else "",
                "current_provider_name": (
                    f"{current.title or ''} {current.full_name or ''}".strip() if current else "—"
                ),
                "providers": [
                    {
                        "id": p.id,
                        "label": f"{p.title or ''} {p.full_name} — {profession_label(p.profession)} ({format_fee(p.fee_ngn, p.currency)})".strip(),
                    }
                    for p in providers
                ],
                "error": msg,
            }
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/telehealth_session_reassign.html",
            ctx,
            status_code=400,
        )

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        if is_created and not data.get("id"):
            data["id"] = new_id("ths")
        now = now_iso()
        if is_created:
            data["created_at"] = now
        data["updated_at"] = now
        prior_billing = getattr(model, "billing_status", None) if not is_created else None
        new_billing = data.get("billing_status", prior_billing)
        session_id = data.get("id") or getattr(model, "id", None)
        if (
            session_id
            and new_billing == "paid"
            and prior_billing != "paid"
        ):
            from app.telehealth import deliver_post_payment_patient_message

            await anyio.to_thread.run_sync(
                lambda: deliver_post_payment_patient_message(str(session_id), force=True)
            )


class TriageSessionAdmin(HCModelView, model=TriageSession):
    category = "Clinical triage"
    column_list = [
        TriageSession.phone_e164,
        TriageSession.status,
        TriageSession.channel,
        TriageSession.chief_complaint,
        TriageSession.expires_at,
        TriageSession.telehealth_session_id,
        TriageSession.created_at,
    ]
    column_searchable_list = [TriageSession.phone_e164, TriageSession.chief_complaint, TriageSession.status]
    column_sortable_list = [TriageSession.created_at, TriageSession.expires_at]
    column_formatters = {
        TriageSession.chief_complaint: _truncate_list_cell("chief_complaint", 48),
    }
    name = "Triage session"
    name_plural = "Triage sessions"


class PatientMediaAttachmentAdmin(HCModelView, model=PatientMediaAttachment):
    category = "Clinical triage"
    column_list = [
        PatientMediaAttachment.id,
        PatientMediaAttachment.phone_e164,
        PatientMediaAttachment.media_type,
        PatientMediaAttachment.media_category,
        PatientMediaAttachment.caption,
        PatientMediaAttachment.processing_status,
        PatientMediaAttachment.triage_session_id,
        PatientMediaAttachment.telehealth_session_id,
        PatientMediaAttachment.created_at,
    ]
    column_searchable_list = [
        PatientMediaAttachment.phone_e164,
        PatientMediaAttachment.caption,
        PatientMediaAttachment.media_category,
        PatientMediaAttachment.sha256,
    ]
    column_sortable_list = [PatientMediaAttachment.created_at]
    column_formatters = {
        PatientMediaAttachment.caption: _truncate_list_cell("caption", 40),
        PatientMediaAttachment.ocr_text: _truncate_list_cell("ocr_text", 48),
        PatientMediaAttachment.vision_summary: _truncate_list_cell("vision_summary", 48),
    }
    form_columns = [
        PatientMediaAttachment.caption,
        PatientMediaAttachment.media_type,
        PatientMediaAttachment.media_category,
        PatientMediaAttachment.ocr_text,
        PatientMediaAttachment.vision_summary,
        PatientMediaAttachment.processing_status,
        PatientMediaAttachment.processing_error,
        PatientMediaAttachment.storage_path,
        PatientMediaAttachment.triage_session_id,
        PatientMediaAttachment.telehealth_session_id,
    ]
    can_create = False
    name = "Media attachment"
    name_plural = "Media attachments"


class TriageAssessmentAdmin(HCModelView, model=TriageAssessment):
    category = "Clinical triage"
    column_list = [
        TriageAssessment.created_at,
        TriageAssessment.phone_e164,
        TriageAssessment.urgency_tier,
        TriageAssessment.risk_level,
        TriageAssessment.protocol_id,
        TriageAssessment.medication_safety,
        TriageAssessment.fraud_signal,
        TriageAssessment.source,
    ]
    column_searchable_list = [
        TriageAssessment.phone_e164,
        TriageAssessment.urgency_tier,
        TriageAssessment.protocol_id,
        TriageAssessment.differential_notes,
    ]
    column_sortable_list = [TriageAssessment.created_at]
    column_formatters = {
        TriageAssessment.patient_text: _truncate_list_cell("patient_text", 48),
        TriageAssessment.differential_notes: _truncate_list_cell("differential_notes", 56),
    }
    can_create = False
    can_edit = False
    name = "Triage assessment"
    name_plural = "Triage assessments"


def _nok_patient_cell(model: PatientNextOfKin, attr: Any) -> str:
    with session_scope() as s:
        u = s.get(User, model.user_id)
    if u is None:
        return model.user_id
    nm = (u.full_name or "").strip()
    ph = (u.phone_e164 or "").strip()
    if nm and ph:
        return f"{nm} · {ph}"
    return nm or ph or model.user_id


class PatientNextOfKinAdmin(HCModelView, model=PatientNextOfKin):
    category = "Clinical triage"
    column_list = [
        PatientNextOfKin.user_id,
        PatientNextOfKin.full_name,
        PatientNextOfKin.phone_e164,
        PatientNextOfKin.relationship,
        PatientNextOfKin.notify_on_emergency,
    ]
    column_formatters = {PatientNextOfKin.user_id: _nok_patient_cell}
    column_labels = {
        PatientNextOfKin.user_id: "Patient (name · phone)",
        PatientNextOfKin.full_name: "Contact name",
        PatientNextOfKin.phone_e164: "Contact phone",
    }
    column_searchable_list = [PatientNextOfKin.full_name, PatientNextOfKin.phone_e164, PatientNextOfKin.user_id]
    form_columns = [
        PatientNextOfKin.user_id,
        PatientNextOfKin.full_name,
        PatientNextOfKin.phone_e164,
        PatientNextOfKin.relationship,
        PatientNextOfKin.notify_on_emergency,
    ]
    form_overrides = {"notify_on_emergency": BooleanField}
    name = "Next of kin (per patient)"
    name_plural = "Next of kin (per patient)"


class GlucoseReadingAdmin(HCModelView, model=GlucoseReading):
    category = "Clinical triage"
    column_list = [
        GlucoseReading.recorded_at,
        GlucoseReading.phone_e164,
        GlucoseReading.value_mg_dl,
        GlucoseReading.reading_type,
        GlucoseReading.source,
    ]
    column_sortable_list = [GlucoseReading.recorded_at, GlucoseReading.value_mg_dl]
    can_create = False
    name = "Glucose reading"
    name_plural = "Glucose readings"


class MedicationAdjustmentDraftAdmin(HCModelView, model=MedicationAdjustmentDraft):
    category = "Clinical triage"
    column_list = [
        MedicationAdjustmentDraft.created_at,
        MedicationAdjustmentDraft.phone_e164,
        MedicationAdjustmentDraft.status,
        MedicationAdjustmentDraft.trend_summary,
    ]
    column_formatters = {
        MedicationAdjustmentDraft.trend_summary: _truncate_list_cell("trend_summary", 56),
    }
    can_create = False
    name = "Medication draft"
    name_plural = "Medication drafts (clinician approval)"


class ClinicalReminderAdmin(HCModelView, model=ClinicalReminder):
    category = "Clinical triage"
    column_list = [
        ClinicalReminder.due_at,
        ClinicalReminder.phone_e164,
        ClinicalReminder.reminder_type,
        ClinicalReminder.status,
    ]
    column_sortable_list = [ClinicalReminder.due_at]
    can_create = False
    name = "Clinical reminder"
    name_plural = "Clinical reminders"


class EhrPatientLinkAdmin(HCModelView, model=EhrPatientLink):
    category = "EHR integration"
    column_list = [
        EhrPatientLink.user_id,
        EhrPatientLink.external_patient_id,
        EhrPatientLink.external_mrn,
        EhrPatientLink.last_synced_at,
        EhrPatientLink.updated_at,
    ]
    column_searchable_list = [EhrPatientLink.external_patient_id, EhrPatientLink.external_mrn]
    name = "EHR patient link"
    name_plural = "EHR patient links"


class EhrSyncLogAdmin(HCModelView, model=EhrSyncLog):
    category = "EHR integration"
    column_list = [
        EhrSyncLog.created_at,
        EhrSyncLog.direction,
        EhrSyncLog.resource_type,
        EhrSyncLog.status,
        EhrSyncLog.resource_id,
        EhrSyncLog.error,
    ]
    column_searchable_list = [EhrSyncLog.resource_type, EhrSyncLog.status, EhrSyncLog.error]
    column_sortable_list = [EhrSyncLog.created_at]
    can_create = False
    can_edit = False
    name = "EHR sync log"
    name_plural = "EHR sync logs"


class ProviderRatingAdmin(HCModelView, model=ProviderRating):
    category = "Telehealth"
    column_list = [
        ProviderRating.provider_id,
        ProviderRating.score,
        ProviderRating.patient_phone_e164,
        ProviderRating.comment,
        ProviderRating.created_at,
    ]
    column_searchable_list = [ProviderRating.provider_id, ProviderRating.patient_phone_e164, ProviderRating.comment]
    column_sortable_list = [ProviderRating.created_at, ProviderRating.score]
    form_columns = [
        ProviderRating.session_id,
        ProviderRating.provider_id,
        ProviderRating.patient_phone_e164,
        ProviderRating.score,
        ProviderRating.comment,
    ]
    form_args = {"score": {"validators": [validators.NumberRange(min=1, max=5)]}}
    can_create = False
    name = "Rating"
    name_plural = "Ratings"


def _normalize_comma_keywords(value: str) -> str:
    parts = [p.strip().upper() for p in value.split(",") if p.strip()]
    return ",".join(parts)


def _normalize_single_keyword(value: str) -> str:
    return value.strip().upper()


def _blank_optional_strings(data: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key not in data or data[key] is None:
            continue
        s = str(data[key]).strip()
        data[key] = s if s else None


def _recent_twilio_delivery_rows() -> list[TwilioDeliveryEvent]:
    with session_scope() as s:
        q = (
            select(TwilioDeliveryEvent)
            .order_by(desc(TwilioDeliveryEvent.created_at))
            .limit(40)
        )
        rows = list(s.execute(q).scalars().all())
        for r in rows:
            s.expunge(r)
        return rows


class TranslationMonitoringView(BaseView):
    """Hub + detail pages for translation cache, learning samples, Marian/Pidgin health."""

    name = "Monitoring overview"
    icon = "fa-solid fa-chart-pie"
    category = "Translation & ML"
    identity = "translation-monitoring"
    _LIVE_TEST_SESSION_KEY = "hc_translation_live_test"
    _SECTIONS: dict[str, tuple[str, str]] = {
        "learning": ("Learning samples", "Totals, trends, and provider breakdown"),
        "cache": ("Translation cache", "Counts, stale rows, and breakdowns"),
        "patients": ("Patients by LANG", "Language preferences across enrollments"),
        "recording": ("Recording learning", "Training sample capture on/off"),
        "live-test": ("Pidgin smoke test", "Marian direct + outbound pipeline"),
        "runtime": ("Runtime configuration", "Model version, API keys, timeouts"),
        "models": ("Models on disk & in RAM", "FastText LID, Hugging Face, Marian"),
        "language-pairs": ("Language pairs", "Top source→target pairs in training data"),
        "recent-learning": ("Recent training samples", "Latest recorded translations"),
        "recent-cache": ("Recent cache updates", "Latest cached translation rows"),
    }

    def _monitoring_context(self, data: dict[str, Any], *, live_test: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "generated_at": data["generated_at"],
            "config": data["config"],
            "assets": data["assets"],
            "counts": data["counts"],
            "cache_by_provider": data["cache_by_provider"],
            "cache_by_category": data["cache_by_category"],
            "learning_by_provider": data["learning_by_provider"],
            "language_pairs": data["language_pairs"],
            "users_by_lang": data["users_by_lang"],
            "recent_learning": data["recent_learning"],
            "recent_cache": data["recent_cache"],
            "live_test": live_test,
        }

    @expose("/translation-monitoring", methods=["GET"], identity="translation-monitoring")
    async def translation_monitoring_page(self, request: Request):
        data = await anyio.to_thread.run_sync(gather_translation_monitoring)
        live_test = request.session.pop(self._LIVE_TEST_SESSION_KEY, None)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/translation_monitoring_hub.html",
            self._monitoring_context(data, live_test=live_test),
        )

    @expose("/translation-monitoring/detail", methods=["GET"], identity="translation_monitoring_detail")
    async def translation_monitoring_detail(self, request: Request):
        section = (request.query_params.get("section") or "").strip()
        if not section:
            return RedirectResponse(url="/admin/translation-monitoring", status_code=302)
        if section not in self._SECTIONS:
            raise HTTPException(status_code=404, detail=f"Unknown monitoring section: {section}")
        data = await anyio.to_thread.run_sync(gather_translation_monitoring)
        live_test: dict[str, Any] | None = None
        if section == "live-test":
            live_test = request.session.pop(self._LIVE_TEST_SESSION_KEY, None)
        page_title, page_subtitle = self._SECTIONS[section]
        ctx = self._monitoring_context(data, live_test=live_test)
        ctx.update(section=section, page_title=page_title, page_subtitle=page_subtitle)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/translation_monitoring_detail.html",
            ctx,
        )

    @expose(
        "/translation-monitoring/run-live-test",
        methods=["GET", "POST"],
        identity="translation_monitoring_live_test",
    )
    async def translation_monitoring_live_test(self, request: Request):
        if request.method == "GET":
            return RedirectResponse(
                url="/admin/translation-monitoring/detail?section=live-test",
                status_code=302,
            )

        live = await anyio.to_thread.run_sync(run_live_pcm_smoke)
        request.session[self._LIVE_TEST_SESSION_KEY] = live
        return RedirectResponse(
            url="/admin/translation-monitoring/detail?section=live-test",
            status_code=303,
        )


class VoiceMessageLogAdmin(HCModelView, model=VoiceMessageLog):
    name = "Voice message logs"
    name_plural = "Voice message logs"
    icon = "fa-solid fa-microphone"
    category = "Translation & ML"
    can_create = False
    can_edit = False
    can_delete = True
    page_size = 50
    column_default_sort = [(VoiceMessageLog.created_at, True)]
    column_list = [
        VoiceMessageLog.created_at,
        VoiceMessageLog.direction,
        VoiceMessageLog.phone_e164,
        VoiceMessageLog.success,
        VoiceMessageLog.provider,
        VoiceMessageLog.error,
        VoiceMessageLog.text_preview,
    ]


class TranslationLearningSampleAdmin(HCModelView, model=TranslationLearningSample):
    name = "Learning samples"
    name_plural = "Learning samples"
    icon = "fa-solid fa-database"
    category = "Translation & ML"
    can_create = False
    can_edit = False
    can_delete = True
    page_size = 50
    column_default_sort = [(TranslationLearningSample.created_at, True)]
    column_list = [
        TranslationLearningSample.created_at,
        TranslationLearningSample.source_lang,
        TranslationLearningSample.target_lang,
        TranslationLearningSample.provider,
        TranslationLearningSample.ml_model_version,
        TranslationLearningSample.category,
        TranslationLearningSample.source_text,
        TranslationLearningSample.output_text,
    ]
    column_formatters = {
        TranslationLearningSample.source_text: _truncate_list_cell("source_text", 48),
        TranslationLearningSample.output_text: _truncate_list_cell("output_text", 48),
    }
    column_searchable_list = [
        TranslationLearningSample.source_lang,
        TranslationLearningSample.target_lang,
        TranslationLearningSample.provider,
        TranslationLearningSample.source_text,
        TranslationLearningSample.output_text,
    ]
    column_labels = {
        TranslationLearningSample.source_text: "Source text (training input)",
        TranslationLearningSample.output_text: "Model output (training target)",
        TranslationLearningSample.ml_model_version: "Model version tag",
    }


class TranslationCacheAdmin(HCModelView, model=TranslationCache):
    name = "Translation cache"
    name_plural = "Translation cache"
    icon = "fa-solid fa-box-archive"
    category = "Translation & ML"
    can_create = False
    can_edit = False
    can_delete = True
    page_size = 50
    column_default_sort = [(TranslationCache.updated_at, True)]
    column_list = [
        TranslationCache.updated_at,
        TranslationCache.category,
        TranslationCache.source_lang,
        TranslationCache.target_lang,
        TranslationCache.provider,
        TranslationCache.ml_model_version,
        TranslationCache.source_text,
        TranslationCache.result_text,
    ]
    column_formatters = {
        TranslationCache.source_text: _truncate_list_cell("source_text", 40),
        TranslationCache.result_text: _truncate_list_cell("result_text", 40),
    }
    column_searchable_list = [
        TranslationCache.source_lang,
        TranslationCache.target_lang,
        TranslationCache.provider,
        TranslationCache.source_text,
        TranslationCache.result_text,
    ]


class TestChatView(BaseView):
    """Sidebar link to the in-app SMS/WhatsApp routing test UI."""

    name = "Test chat"
    icon = "fa-solid fa-comments"
    category = "Setup"

    @expose("/test-chat", methods=["GET"])
    async def test_chat_redirect(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/test/chat", status_code=302)


class WebhooksReferenceView(BaseView):
    """Admin page: copy webhook URLs and run safe self-tests (HTTP back to this app)."""

    name = "Webhooks reference"
    icon = "fa-solid fa-link"
    category = "Setup"

    @expose("/webhooks-reference", methods=["GET"])
    async def webhooks_reference_page(self, request: Request):
        from app.integration_config import get_effective_integration

        integ = get_effective_integration()
        base = integ.hc_base_url.rstrip("/")
        rows = await anyio.to_thread.run_sync(_recent_twilio_delivery_rows)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/webhooks_reference.html",
            {
                "base_url": base,
                "meta_verify_token_set": bool((integ.meta_wa_verify_token or "").strip()),
                "twilio_delivery_events": rows,
            },
        )

    @staticmethod
    def _test_result_page(title: str, ok: bool, lines: list[str], back: str) -> HTMLResponse:
        color = "#047857" if ok else "#b91c1c"
        body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"/><title>{html.escape(title)}</title></head>
<body style="font-family:system-ui;max-width:48rem;margin:2rem auto;line-height:1.5">
<h1>{html.escape(title)}</h1>
<p><strong style="color:{color}">{"OK" if ok else "Failed"}</strong></p>
{body}
<p style="margin-top:2rem"><a href="{html.escape(back)}">← Webhooks reference</a></p>
</body></html>"""
        )

    @expose("/webhooks-reference/test-meta-verify", methods=["POST"])
    async def webhooks_test_meta_verify(self, request: Request) -> HTMLResponse:
        ok, lines = await anyio.to_thread.run_sync(test_meta_webhook_verify)
        return self._test_result_page("Meta webhook (GET verify)", ok, lines, "/admin/webhooks-reference")

    @expose("/webhooks-reference/test-twilio-status", methods=["POST"])
    async def webhooks_test_twilio_status(self, request: Request) -> HTMLResponse:
        ok, lines = await anyio.to_thread.run_sync(test_twilio_status_webhook)
        return self._test_result_page("Twilio status webhook", ok, lines, "/admin/webhooks-reference")

    @expose("/webhooks-reference/test-twilio-inbound", methods=["POST"])
    async def webhooks_test_twilio_inbound(self, request: Request) -> HTMLResponse:
        ok, lines = await anyio.to_thread.run_sync(test_twilio_inbound_webhook)
        return self._test_result_page("Twilio inbound webhook", ok, lines, "/admin/webhooks-reference")


def _broadcast_program_label(code: str) -> str:
    if code == "all":
        return "All programmes"
    for value, label in fc.PROGRAM_CHOICES:
        if value == code:
            return label
    return code


def _broadcast_channel_label(code: Optional[str]) -> str:
    if not code:
        return "SMS + WhatsApp"
    for value, label in fc.BROADCAST_CHANNEL_CHOICES:
        if value == code:
            return label
    return code


def _parse_broadcast_form(form: dict[str, str]) -> tuple[BroadcastFilters, str, bool]:
    filters = BroadcastFilters(
        program=(form.get("program") or "all").strip(),
        enrollment_status=(form.get("enrollment_status") or "active").strip(),
        channel=(form.get("channel") or "").strip() or None,
        language=(form.get("language") or "").strip() or None,
    )
    message_body = (form.get("message_body") or "").strip()
    translate_per_user = form.get("translate_per_user") in ("1", "on", "true", "yes")
    return filters, message_body, translate_per_user


class BroadcastView(BaseView):
    """Helpdesk / ops: send a message to all members in a programme group."""

    name = "Broadcast"
    icon = "fa-solid fa-bullhorn"
    category = "Messaging"
    identity = "broadcast"

    def _compose_context(
        self,
        *,
        form: dict[str, Any],
        flash: dict[str, str] | None = None,
        preview: dict[str, Any] | None = None,
        last_run_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "program_choices": fc.BROADCAST_PROGRAM_CHOICES,
            "status_choices": fc.ENROLLMENT_STATUS_CHOICES,
            "channel_choices": fc.BROADCAST_CHANNEL_CHOICES,
            "language_choices": fc.BROADCAST_LANGUAGE_CHOICES,
            "form": form,
            "flash": flash,
            "preview": preview,
            "recent_runs": list_recent_broadcast_runs(),
            "last_run_id": last_run_id,
        }

    @expose("/broadcast/run", methods=["GET"], identity="broadcast-run-detail")
    async def broadcast_run_detail(self, request: Request):
        run_id = (request.query_params.get("run_id") or "").strip()
        if not run_id:
            raise HTTPException(status_code=400, detail="run_id is required")
        detail = await anyio.to_thread.run_sync(lambda: get_broadcast_run_detail(run_id))
        if not detail:
            raise HTTPException(status_code=404, detail="Broadcast not found")
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/broadcast_run_detail.html",
            detail,
        )

    @expose("/broadcast", methods=["GET", "POST"], identity="broadcast")
    async def broadcast_compose(self, request: Request):
        default_form = {
            "program": "all",
            "enrollment_status": "active",
            "channel": "",
            "language": "",
            "message_body": "",
            "translate_per_user": True,
        }
        if request.method == "GET":
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/broadcast_compose.html",
                self._compose_context(form=default_form),
            )

        fd = {k: str(v) for k, v in (await request.form()).items()}
        action = (fd.get("action") or "preview").strip().lower()
        filters, message_body, translate_per_user = _parse_broadcast_form(fd)
        form = {
            "program": filters.program,
            "enrollment_status": filters.enrollment_status,
            "channel": filters.channel or "",
            "language": filters.language or "",
            "message_body": message_body,
            "translate_per_user": translate_per_user,
        }

        try:
            if action == "send":
                if not message_body:
                    raise ValueError("Message body is required.")
                admin_user = str(request.session.get("hc_admin_user") or "").strip() or None
                run_id, audience_count, queued_count = await anyio.to_thread.run_sync(
                    lambda: enqueue_broadcast(
                        filters=filters,
                        message_body=message_body,
                        translate_per_user=translate_per_user,
                        created_by=admin_user,
                    )
                )
                sent_now = 0
                failed_now = 0
                batch_summaries: list[dict[str, Any]] = []
                if queued_count:
                    sent_now, failed_now, batch_summaries = await anyio.to_thread.run_sync(
                        lambda: deliver_broadcast_run(run_id=run_id)
                    )
                level = "success" if failed_now == 0 else "warning"
                batch_text = ""
                if batch_summaries:
                    parts = [
                        f"{b['label']}: {b['sent']}/{b['total']}"
                        for b in batch_summaries
                    ]
                    batch_text = f" Sent by language (English first): {', '.join(parts)}."
                flash = {
                    "level": level,
                    "message": (
                        f"Broadcast queued {queued_count} message(s) for {audience_count} member(s). "
                        f"Delivered {sent_now} now"
                        + (f", {failed_now} failed (see delivery status for errors)" if failed_now else "")
                        + batch_text
                    ),
                }
                ctx = self._compose_context(form=default_form, flash=flash)
                ctx["last_run_id"] = run_id
                return await self.templates.TemplateResponse(
                    request,
                    "sqladmin/broadcast_compose.html",
                    ctx,
                )

            count = await anyio.to_thread.run_sync(lambda: count_broadcast_recipients(filters))
            sample = await anyio.to_thread.run_sync(lambda: preview_broadcast_recipients(filters))
            language_counts = await anyio.to_thread.run_sync(
                lambda: audience_counts_by_language(filters)
            )
            wa_session = await anyio.to_thread.run_sync(
                lambda: audience_whatsapp_session_stats(filters)
            )
            preview = {
                "count": count,
                "sample": sample,
                "language_counts": language_counts,
                "wa_session": wa_session,
            }
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/broadcast_compose.html",
                self._compose_context(form=form, preview=preview),
            )
        except ValueError as exc:
            flash = {"level": "danger", "message": str(exc)}
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/broadcast_compose.html",
                self._compose_context(form=form, flash=flash),
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("broadcast send failed")
            flash = {"level": "danger", "message": f"Broadcast failed: {exc}"}
            return await self.templates.TemplateResponse(
                request,
                "sqladmin/broadcast_compose.html",
                self._compose_context(form=form, flash=flash),
            )


class BroadcastRunAdmin(HCModelView, model=BroadcastRun):
    can_create = False
    can_edit = False
    can_delete = False
    name = "Broadcast history"
    name_plural = "Broadcast history"
    icon = "fa-solid fa-clock-rotate-left"
    category = "Messaging"

    column_list = [
        BroadcastRun.created_at,
        BroadcastRun.id,
        BroadcastRun.program,
        BroadcastRun.enrollment_status,
        BroadcastRun.channel_filter,
        BroadcastRun.queued_count,
        BroadcastRun.recipient_count,
        BroadcastRun.created_by,
        BroadcastRun.message_body,
    ]
    column_sortable_list = [BroadcastRun.created_at]
    column_default_sort = [(BroadcastRun.created_at, True)]
    column_formatters = {
        BroadcastRun.id: lambda m, a: Markup(
            f'<a href="/admin/broadcast/run?run_id={html.escape(m.id)}">Recipients</a>'
        ),
        BroadcastRun.program: lambda m, a: _broadcast_program_label(m.program),
        BroadcastRun.channel_filter: lambda m, a: _broadcast_channel_label(m.channel_filter),
        BroadcastRun.message_body: _truncate_list_cell("message_body", 64),
    }
    column_labels = {
        BroadcastRun.queued_count: "Queued",
        BroadcastRun.recipient_count: "Audience",
        BroadcastRun.channel_filter: "Channel",
        BroadcastRun.language_filter: "Language",
    }


class IntegrationSelfTestView(BaseView):
    """Hidden routes for POST self-tests (linked from Integration settings edit page)."""

    name = "Integration self-tests"
    icon = "fa-solid fa-flask"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/integration-self-test/run-meta", methods=["POST"])
    async def run_meta_test(self, request: Request) -> HTMLResponse:
        back = safe_back_url(request.headers.get("referer"))
        out = await anyio.to_thread.run_sync(test_meta_whatsapp)
        return HTMLResponse(format_outcome_html(out, back_url=back))

    @expose("/integration-self-test/run-twilio", methods=["POST"])
    async def run_twilio_test(self, request: Request) -> HTMLResponse:
        back = safe_back_url(request.headers.get("referer"))
        out = await anyio.to_thread.run_sync(test_twilio)
        return HTMLResponse(format_outcome_html(out, back_url=back))

    @expose("/integration-self-test/run-at", methods=["POST"])
    async def run_at_test(self, request: Request) -> HTMLResponse:
        back = safe_back_url(request.headers.get("referer"))
        out = await anyio.to_thread.run_sync(test_africastalking)
        return HTMLResponse(format_outcome_html(out, back_url=back))

    @expose("/integration-self-test/run-broadcast-template", methods=["POST"])
    async def run_broadcast_template_test(self, request: Request) -> HTMLResponse:
        back = safe_back_url(request.headers.get("referer"))
        out = await anyio.to_thread.run_sync(test_broadcast_template)
        return HTMLResponse(format_outcome_html(out, back_url=back))

    @expose("/integration-self-test/run-paystack", methods=["POST"])
    async def run_paystack_test(self, request: Request) -> HTMLResponse:
        back = safe_back_url(request.headers.get("referer"))
        out = await anyio.to_thread.run_sync(test_paystack)
        return HTMLResponse(format_outcome_html(out, back_url=back))


class IntegrationSettingsAdmin(HCModelView, model=IntegrationSettings):
    list_template = "sqladmin/integration_settings_list.html"
    edit_template = "sqladmin/integration_settings_edit.html"
    column_list = [
        IntegrationSettings.id,
        IntegrationSettings.hc_base_url,
        IntegrationSettings.hc_whatsapp_number,
        IntegrationSettings.hc_sms_join_number,
        IntegrationSettings.helpdesk_phone_e164,
        IntegrationSettings.on_call_clinician_phone_e164,
        IntegrationSettings.meta_wa_phone_number_id,
        IntegrationSettings.updated_at,
    ]
    column_formatters = {
        IntegrationSettings.hc_base_url: _truncate_list_cell("hc_base_url", 72),
        IntegrationSettings.hc_whatsapp_number: _truncate_list_cell("hc_whatsapp_number", 24),
        IntegrationSettings.hc_sms_join_number: _truncate_list_cell("hc_sms_join_number", 24),
        IntegrationSettings.helpdesk_phone_e164: _truncate_list_cell("helpdesk_phone_e164", 22),
        IntegrationSettings.meta_wa_phone_number_id: _truncate_list_cell("meta_wa_phone_number_id", 18),
    }
    column_sortable_list = [IntegrationSettings.updated_at]
    form_columns = [
        IntegrationSettings.hc_base_url,
        IntegrationSettings.hc_whatsapp_number,
        IntegrationSettings.hc_sms_join_number,
        IntegrationSettings.helpdesk_phone_e164,
        IntegrationSettings.on_call_clinician_phone_e164,
        IntegrationSettings.meta_wa_phone_number_id,
        IntegrationSettings.meta_wa_waba_id,
        IntegrationSettings.meta_wa_access_token,
        IntegrationSettings.meta_wa_verify_token,
        IntegrationSettings.meta_app_secret,
        IntegrationSettings.meta_graph_version,
        IntegrationSettings.meta_wa_broadcast_template,
        IntegrationSettings.meta_wa_broadcast_template_language,
        IntegrationSettings.twilio_account_sid,
        IntegrationSettings.twilio_auth_token,
        IntegrationSettings.twilio_messaging_service_sid,
        IntegrationSettings.at_username,
        IntegrationSettings.at_api_key,
        IntegrationSettings.at_shortcode,
        IntegrationSettings.operator_webhook_url,
        IntegrationSettings.paystack_secret_key,
        IntegrationSettings.paystack_public_key,
        IntegrationSettings.meta_wa_calling_enabled,
        IntegrationSettings.meta_wa_calling_webrtc_bridge_url,
    ]
    column_labels = {
        IntegrationSettings.hc_base_url: "Public base URL (https://your-host; no trailing slash). Used for QR, join links, webhook hints.",
        IntegrationSettings.hc_whatsapp_number: "Digits only, no + — wa.me fallback when Meta Cloud API is off or Graph lookup fails; with Meta configured, join uses Meta’s display number.",
        IntegrationSettings.hc_sms_join_number: "SMS number patients text JOIN to (E.164 or digits). Your Twilio number that receives SMS and hits /webhooks/twilio/inbound (see Messaging Service senders).",
        IntegrationSettings.helpdesk_phone_e164: "Helpdesk staff phone (E.164 or digits). New helpdesk tickets and telehealth bookings are sent here. Staff replies with: TKT <ticket_id> message. Env HC_HELPDESK_PHONE if unset here.",
        IntegrationSettings.on_call_clinician_phone_e164: "On-call clinician (E.164) — emergency triage alerts with patient record. Env HC_ON_CALL_CLINICIAN_PHONE.",
        IntegrationSettings.meta_wa_phone_number_id: "Meta: WhatsApp → API setup → Phone number ID.",
        IntegrationSettings.meta_wa_waba_id: "Meta: WhatsApp Business Account ID (optional reference).",
        IntegrationSettings.meta_wa_access_token: "Meta: permanent system user token (WhatsApp permissions).",
        IntegrationSettings.meta_wa_verify_token: "Meta webhook verify token (must match Meta developer console).",
        IntegrationSettings.meta_app_secret: "Meta app secret — enables X-Hub-Signature-256 validation on inbound POST.",
        IntegrationSettings.meta_graph_version: "Graph API version, e.g. v25.0",
        IntegrationSettings.meta_wa_broadcast_template: "Approved Meta template name for proactive WhatsApp broadcasts (utility template with {{1}} = message body). Required for users who have not messaged in 24h.",
        IntegrationSettings.meta_wa_broadcast_template_language: "Template language code from Meta (usually en_US for English, not en)",
        IntegrationSettings.twilio_account_sid: "Twilio Account SID (SMS/WhatsApp if not using Meta for WA).",
        IntegrationSettings.twilio_auth_token: "Twilio auth token.",
        IntegrationSettings.twilio_messaging_service_sid: "Twilio Messaging Service SID.",
        IntegrationSettings.at_username: "Africa's Talking username.",
        IntegrationSettings.at_api_key: "Africa's Talking API key.",
        IntegrationSettings.at_shortcode: "Africa's Talking sender ID / short code.",
        IntegrationSettings.operator_webhook_url: "POST JSON on helpdesk + telehealth events (telehealth.session.booked, telehealth.session.paid). Env HC_OPERATOR_WEBHOOK_URL if unset here.",
        IntegrationSettings.paystack_secret_key: "Paystack secret key (sk_live_… / sk_test_…). Enables telehealth payment links. Env PAYSTACK_SECRET_KEY.",
        IntegrationSettings.paystack_public_key: "Paystack public key (pk_…) for client-side checkout if needed. Env PAYSTACK_PUBLIC_KEY.",
        IntegrationSettings.meta_wa_calling_enabled: "Enable Meta WhatsApp Calling API for business-initiated video after payment (requires calls webhook + WebRTC bridge or SDP).",
        IntegrationSettings.meta_wa_calling_webrtc_bridge_url: "Optional HTTP endpoint POST {session_id,to} → {sdp} for Meta /calls connect. Env HC_META_WA_CALLING_WEBRTC_BRIDGE_URL.",
    }
    form_overrides = {
        "meta_wa_calling_enabled": BooleanField,
    }
    can_create = False
    can_delete = False
    name = "Deployment & integrations"
    name_plural = "Deployment & integrations"

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        if not isinstance(model, IntegrationSettings):
            return
        data["updated_at"] = now_iso()
        _blank_optional_strings(
            data,
            (
                "hc_base_url",
                "hc_whatsapp_number",
                "hc_sms_join_number",
                "helpdesk_phone_e164",
                "on_call_clinician_phone_e164",
                "meta_wa_phone_number_id",
                "meta_wa_waba_id",
                "meta_wa_verify_token",
                "meta_wa_broadcast_template",
                "meta_wa_broadcast_template_language",
                "twilio_account_sid",
                "twilio_messaging_service_sid",
                "at_username",
                "at_shortcode",
                "operator_webhook_url",
                "meta_wa_calling_webrtc_bridge_url",
            ),
        )
        for secret_field in (
            "meta_wa_access_token",
            "meta_app_secret",
            "twilio_auth_token",
            "at_api_key",
            "paystack_secret_key",
            "paystack_public_key",
        ):
            if not str(data.get(secret_field) or "").strip():
                data[secret_field] = getattr(model, secret_field, None)
        if "meta_graph_version" in data and data["meta_graph_version"] is not None:
            v = str(data["meta_graph_version"]).strip()
            data["meta_graph_version"] = v if v else "v25.0"
        if data.get("hc_base_url"):
            data["hc_base_url"] = str(data["hc_base_url"]).strip().rstrip("/")


class AppSettingsAdmin(HCModelView, model=AppSettings):
    column_list = [
        AppSettings.id,
        AppSettings.ussd_code,
        AppSettings.sms_join_keywords,
        AppSettings.whatsapp_join_keywords,
        AppSettings.auto_reply_enabled,
    ]
    column_formatters = {
        AppSettings.sms_join_keywords: _truncate_list_cell("sms_join_keywords", 44),
        AppSettings.whatsapp_join_keywords: _truncate_list_cell("whatsapp_join_keywords", 44),
    }
    form_columns = [
        AppSettings.ussd_code,
        AppSettings.ussd_welcome_message,
        AppSettings.ussd_invalid_choice_message,
        AppSettings.sms_join_keywords,
        AppSettings.sms_help_keywords,
        AppSettings.whatsapp_join_keywords,
        AppSettings.whatsapp_help_keywords,
        AppSettings.symptom_check_keywords,
        AppSettings.lang_command_keyword,
        AppSettings.stop_keywords,
        AppSettings.start_keywords,
        AppSettings.appoint_keyword,
        AppSettings.escalate_keyword,
        AppSettings.rpm_keyword,
        AppSettings.telehealth_keyword,
        AppSettings.rate_keyword,
        AppSettings.triage_keyword,
        AppSettings.reschedule_keyword,
        AppSettings.auto_reply_enabled,
        AppSettings.whatsapp_voice_inbound_enabled,
        AppSettings.whatsapp_voice_outbound_enabled,
        AppSettings.whatsapp_voice_outbound_max_chars,
        AppSettings.default_help_menu_text,
        AppSettings.unknown_fallback_text,
    ]
    column_labels = {
        AppSettings.id: "Row id (always 1) — this row is what get_app_settings() reads live",
        AppSettings.sms_join_keywords: "SMS join keywords (comma-separated)",
        AppSettings.sms_help_keywords: "SMS help keywords (comma-separated). Default INFO,MENU,COMMANDS — avoid HELP (often reserved on Twilio SMS).",
        AppSettings.whatsapp_join_keywords: "WhatsApp join keywords (comma-separated)",
        AppSettings.whatsapp_help_keywords: "WhatsApp help keywords (comma-separated). Default INFO,MENU,COMMANDS (avoid HELP if you mirror SMS policy).",
        AppSettings.symptom_check_keywords: "Keywords to show symptom-checker instructions (comma-separated; default CHECK,SYMPTOMS). Same list for SMS and WhatsApp.",
        AppSettings.stop_keywords: "Stop keywords (comma-separated)",
        AppSettings.start_keywords: "Start keywords (comma-separated)",
        AppSettings.whatsapp_voice_inbound_enabled: "WhatsApp: accept voice notes (STT → same routing as text)",
        AppSettings.whatsapp_voice_outbound_enabled: "WhatsApp: allow VOICE YES patients to get TTS voice replies",
        AppSettings.whatsapp_voice_outbound_max_chars: "Max reply length for voice (longer replies stay text)",
    }
    can_create = False
    can_delete = False
    name = "Channel Settings"
    name_plural = "Channel Settings"

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        if not isinstance(model, AppSettings):
            return

        for key in (
            "sms_join_keywords",
            "sms_help_keywords",
            "whatsapp_join_keywords",
            "whatsapp_help_keywords",
            "symptom_check_keywords",
            "stop_keywords",
            "start_keywords",
        ):
            if key in data and data[key] is not None:
                normalized = _normalize_comma_keywords(str(data[key]))
                if not normalized:
                    raise HTTPException(status_code=400, detail=f"{key} must contain at least one keyword.")
                data[key] = normalized

        for key in (
            "lang_command_keyword",
            "appoint_keyword",
            "escalate_keyword",
            "rpm_keyword",
            "telehealth_keyword",
            "rate_keyword",
            "triage_keyword",
            "reschedule_keyword",
        ):
            if key in data and data[key] is not None:
                v = _normalize_single_keyword(str(data[key]))
                if not v:
                    raise HTTPException(status_code=400, detail=f"{key} cannot be empty.")
                data[key] = v

        if "ussd_code" in data and data["ussd_code"] is not None:
            data["ussd_code"] = str(data["ussd_code"]).strip()


def mount_admin(app) -> None:
    """
    Mounts /admin with username/password auth from env.
    """
    _templates = str(Path(__file__).resolve().parent / "templates")
    admin = Admin(
        app=app,
        engine=engine,
        title="Kareconnect Admin",
        authentication_backend=SimpleAuth(secret_key=settings.admin_session_secret),
        templates_dir=_templates,
    )
    install_jinja_datetime_filters(admin.templates.env)
    admin.add_view(AdminUserAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(EnrollmentAdmin)
    admin.add_view(MessageOutboxAdmin)
    admin.add_base_view(BroadcastView)
    admin.add_view(BroadcastRunAdmin)
    admin.add_view(InboundMessageAdmin)
    admin.add_view(HelpdeskTicketAdmin)
    admin.add_base_view(AppointmentCalendarView)
    for _view in admin.views:
        if isinstance(_view, AppointmentCalendarView):
            # Last @expose overwrites class identity to appointment-assign (menu 400 without pk).
            _view.identity = "appointment"
            break
    admin.add_view(AppointmentAdmin)
    admin.add_view(EscalationAdmin)
    admin.add_view(TelehealthProviderAdmin)
    admin.add_view(TelehealthSessionAdmin)
    admin.add_view(ProviderRatingAdmin)
    admin.add_view(TriageSessionAdmin)
    admin.add_view(PatientMediaAttachmentAdmin)
    admin.add_view(TriageAssessmentAdmin)
    admin.add_view(PatientNextOfKinAdmin)
    admin.add_view(GlucoseReadingAdmin)
    admin.add_view(MedicationAdjustmentDraftAdmin)
    admin.add_view(ClinicalReminderAdmin)
    admin.add_view(EhrPatientLinkAdmin)
    admin.add_view(EhrSyncLogAdmin)
    admin.add_view(RpmEnrollmentAdmin)
    admin.add_view(IntegrationSettingsAdmin)
    admin.add_base_view(TranslationMonitoringView)
    for _view in admin.views:
        if isinstance(_view, TranslationMonitoringView):
            _view.identity = "translation-monitoring"
            break
    admin.add_view(VoiceMessageLogAdmin)
    admin.add_view(TranslationLearningSampleAdmin)
    admin.add_view(TranslationCacheAdmin)
    admin.add_base_view(TestChatView)
    admin.add_base_view(WebhooksReferenceView)
    admin.add_base_view(IntegrationSelfTestView)
    admin.add_view(TwilioDeliveryEventAdmin)
    admin.add_view(AppSettingsAdmin)

