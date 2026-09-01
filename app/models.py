from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.default_help_menu import DEFAULT_HELP_MENU_TEXT


class Base(DeclarativeBase):
    pass


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ussd_code: Mapped[str] = mapped_column(String(32), nullable=False, default="*134*550*2#")
    ussd_welcome_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="Welcome to Kareconnect\n1. Join Maternal/Child\n2. Join Chronic\n3. Join Youth\n4. Join Kidney",
    )
    ussd_invalid_choice_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="Invalid choice.\n1 Maternal/Child\n2 Chronic\n3 Youth\n4 Kidney",
    )

    sms_join_keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="JOIN")
    sms_help_keywords: Mapped[str] = mapped_column(
        String(255), nullable=False, default="INFO,MENU,COMMANDS"
    )
    whatsapp_join_keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="JOIN")
    whatsapp_help_keywords: Mapped[str] = mapped_column(
        String(255), nullable=False, default="INFO,MENU,COMMANDS"
    )
    # Patient keywords (SMS & WhatsApp) that show how to use the symptom / health-question flow (same list both channels).
    symptom_check_keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="CHECK,SYMPTOMS")

    lang_command_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="LANG")
    stop_keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="STOP")
    start_keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="START")
    appoint_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="APPOINT")
    escalate_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="ESCALATE")
    rpm_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="RPM")
    telehealth_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="TELEHEALTH")
    rate_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="RATE")
    triage_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="TRIAGE")
    reschedule_keyword: Mapped[str] = mapped_column(String(32), nullable=False, default="RESCHEDULE")

    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_help_menu_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=DEFAULT_HELP_MENU_TEXT,
    )
    unknown_fallback_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=(
            "We could not match that. Describe your symptoms in a short message, "
            "or send Helpdesk plus your question to reach our team. "
            "Send INFO, MENU, or COMMANDS for all keywords."
        ),
    )
    # WhatsApp voice notes (Phase 1–3): STT on inbound audio; TTS outbound when patient sends VOICE YES.
    whatsapp_voice_inbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    whatsapp_voice_outbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    whatsapp_voice_outbound_max_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=400)


class IntegrationSettings(Base):
    """
    Singleton row (id=1): deployment URLs and channel credentials.
    Non-empty values override environment variables at runtime.
    """

    __tablename__ = "integration_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hc_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hc_whatsapp_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Patient-facing SMS destination for JOIN (Twilio number or long code on your inbound webhook)
    hc_sms_join_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    meta_wa_phone_number_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta_wa_waba_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta_wa_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_wa_verify_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    meta_app_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_graph_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v25.0")
    # Approved utility/marketing template for proactive WhatsApp broadcasts (BODY {{1}} = message).
    meta_wa_broadcast_template: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    meta_wa_broadcast_template_language: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    twilio_account_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    twilio_auth_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    twilio_messaging_service_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    at_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    at_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    at_shortcode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    operator_webhook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # E.164 or digits — staff number that receives new-ticket alerts on the same channel as the patient
    # and may reply with: TKT <ticket_id> <message>
    helpdesk_phone_e164: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    on_call_clinician_phone_e164: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    paystack_secret_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paystack_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_wa_calling_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Optional HTTP endpoint that returns WebRTC SDP for business-initiated Meta calls.
    meta_wa_calling_webrtc_bridge_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Collected once, on first telehealth booking, for the KareVault appointment payload.
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # male|female|other
    dob: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # ISO date YYYY-MM-DD
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # sms|whatsapp|ussd|web
    language: Mapped[str] = mapped_column(String(8), nullable=False)  # en|pcm|ha|ig|yo
    # Opt-in for automated programme education tips; delivery is WhatsApp-only (see enqueue_due_messages).
    recurring_messages_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    whatsapp_voice_replies_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ISO date (UTC) when we last appended the "Need our team? … Helpdesk" hint on symptom replies.
    last_helpdesk_hint_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    enrollments = relationship("Enrollment", back_populates="user", cascade="all, delete-orphan")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    program: Mapped[str] = mapped_column(String(32), nullable=False)  # maternal_child|chronic|youth|kidney
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # active|paused|completed
    edd: Mapped[str] = mapped_column(String(16), nullable=True)  # ISO date
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    user = relationship("User", back_populates="enrollments")

    __table_args__ = (
        Index("uq_enrollment_user_program", "user_id", "program", unique=True),
    )


class BroadcastRun(Base):
    """Audit log for staff programme broadcasts sent from admin."""

    __tablename__ = "broadcast_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    program: Mapped[str] = mapped_column(String(32), nullable=False)  # all|maternal_child|…
    enrollment_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    channel_filter: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    language_filter: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    translate_per_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class MessageOutbox(Base):
    __tablename__ = "messages_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    program: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    to_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # queued|sent|failed
    error: Mapped[str] = mapped_column(Text, nullable=True)
    broadcast_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("broadcast_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[str] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_outbox_due", "status", "due_at"),
    )


class TwilioDeliveryEvent(Base):
    """
    Twilio Messaging status callbacks (delivery receipts).
    Configure Status callback URL on your Messaging Service or per-number to POST here.
    """

    __tablename__ = "twilio_delivery_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_sid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    to_value: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    from_value: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class VoiceMessageLog(Base):
    """Observability for WhatsApp voice STT/TTS (retention: operational debugging)."""

    __tablename__ = "voice_message_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # inbound|outbound
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    meta_message_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    text_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="google")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    from_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    to_phone_e164: Mapped[str] = mapped_column(String(32), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class HelpdeskTicket(Base):
    __tablename__ = "helpdesk_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    from_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # open|answered|escalated|closed
    suggested_answer: Mapped[str] = mapped_column(Text, nullable=True)
    # KareVault symptom-checker / AI chat (when HC_KAREVAULT_ENABLED); not a clinical record.
    ai_symptom_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    visit_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_person", index=True
    )  # in_person|telehealth
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_date: Mapped[str] = mapped_column(String(16), nullable=True)
    scheduled_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    profession: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("telehealth_providers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    telehealth_session_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("telehealth_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    ehr_appointment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    ehr_sync_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )  # pending|synced|failed|skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # requested|booked|cancelled
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AppointmentPatientFlow(Base):
    """Multi-step APPOINT keyword state per patient phone."""

    __tablename__ = "appointment_patient_flows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="whatsapp")
    step: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle"
    )  # idle|visit_type|reason|profession|provider|confirm|preferred_date|clinic_confirm
    # |specialty|doctor|slot_date|slot_time|patient_email|patient_gender|patient_dob (live-API video path)
    visit_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # telehealth|in_person
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profession_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    selected_provider_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Live-API video-booking path (see app.telehealth's specialty/doctor/slot steps).
    selected_specialty: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    selected_doctor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # KareVault doctor id
    selected_slot_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    selected_slot_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    preferred_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pending_appointment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # routine|urgent|emergency
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # requested|in_progress|resolved
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class TranslationCache(Base):
    """
    Persisted translations: cache hits first, then first-party ML (provider=ml), then Google (provider=google).
    Rows are keyed by hash(category, source_lang, target_lang, normalized source text).
    """

    __tablename__ = "translation_cache"
    __table_args__ = (Index("ix_translation_cache_category_target", "category", "target_lang"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA-256 hex of cache inputs
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # language_detect | to_en | from_en
    source_lang: Mapped[str] = mapped_column(String(16), nullable=False)
    target_lang: Mapped[str] = mapped_column(String(16), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    result_text: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="google")  # ml | google
    ml_model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class TranslationLearningSample(Base):
    """
    Append-only samples for retraining / continuous improvement of your translation ML.
    Written when a translation is resolved (ML or Google) and HC_TRANSLATION_RECORD_LEARNING is on.
    """

    __tablename__ = "translation_learning_samples"
    __table_args__ = (Index("ix_tls_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cache_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    source_lang: Mapped[str] = mapped_column(String(16), nullable=False)
    target_lang: Mapped[str] = mapped_column(String(16), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # ml | google
    ml_model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class RpmEnrollment(Base):
    __tablename__ = "rpm_enrollments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    condition: Mapped[str] = mapped_column(String(64), nullable=False)
    device_type: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # requested|enrolled|declined
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class TelehealthProvider(Base):
    """Registered clinician offering paid telehealth consultations."""

    __tablename__ = "telehealth_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # Dr., Nurse, etc.
    profession: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Set only for rows synced from the KareVault Telehealth API (see app.telehealth's
    # sync_local_provider_from_remote_doctor); NULL for locally hand-entered/demo providers.
    external_doctor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True, unique=True)
    bio: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    license_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    contact_phone_e164: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    fee_ngn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="NGN")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)  # draft|active|inactive
    mask_patient_number: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    avg_rating: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rating_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    sessions = relationship("TelehealthSession", back_populates="provider")
    ratings = relationship("ProviderRating", back_populates="provider")


class TelehealthSession(Base):
    """Patient booking for a telehealth consultation (billing + call coordination)."""

    __tablename__ = "telehealth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    patient_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("telehealth_providers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # sms|whatsapp
    profession_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="confirmed", index=True
    )  # pending_payment|confirmed|in_progress|completed|cancelled|no_show
    fee_ngn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="NGN")
    billing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )  # pending|paid|waived|failed|refunded
    billing_reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    billing_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payment_bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payment_account_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    paystack_customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    call_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="whatsapp_video")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="routine")  # routine|urgent|emergency
    call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    call_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    call_started_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    call_ended_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_joined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feedback_prompt_sent_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payment_instructions_sent_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    call_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mask_patient_number: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The patient's chosen slot, captured at booking time regardless of payment status, and
    # the KareVault Telehealth API's own appointment id/video link once create_appointment
    # has actually been called for this session (immediately for waived, on payment for paid).
    appointment_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # YYYY-MM-DD
    appointment_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # HH:MM
    external_appointment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_call_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    provider = relationship("TelehealthProvider", back_populates="sessions")
    rating = relationship("ProviderRating", back_populates="session", uselist=False)


class ProviderRating(Base):
    __tablename__ = "provider_ratings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("telehealth_sessions.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("telehealth_providers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    patient_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)  # 1–5
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    session = relationship("TelehealthSession", back_populates="rating")
    provider = relationship("TelehealthProvider", back_populates="ratings")


class TelehealthPatientFlow(Base):
    """Multi-step TELEHEALTH keyword state per patient phone."""

    __tablename__ = "telehealth_patient_flows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    step: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")  # idle|profession|provider|confirm
    # |specialty|doctor|slot_date|slot_time|patient_email|patient_gender|patient_dob (live-API path)
    profession_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    selected_provider_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Live-API path (see app.telehealth's specialty/doctor/slot steps).
    selected_specialty: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    selected_doctor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # KareVault doctor id
    selected_slot_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    selected_slot_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    pending_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class TriageSession(Base):
    """Active post-JOIN clinical intake window (photos, documents, reasoning)."""

    __tablename__ = "triage_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)  # active|closed
    chief_complaint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intake_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="ask_age_group")
    intake_profile_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intake_completed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    telehealth_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    helpdesk_ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    closed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class PatientMediaAttachment(Base):
    """Inbound image/document from patient during triage or telehealth."""

    __tablename__ = "patient_media_attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # image|document|video
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_media_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    triage_session_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("triage_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    telehealth_session_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("telehealth_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    helpdesk_ticket_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    media_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # wound|rash|eye|document|other
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vision_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vision_labels_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )  # pending|processed|failed
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class TriageAssessment(Base):
    """Clinical intelligence output (urgency, risk, protocol, medication, fraud)."""

    __tablename__ = "triage_assessments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    triage_session_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("triage_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # text|media|combined
    patient_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    urgency_tier: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # emergency|urgent|routine
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # low|moderate|high
    protocol_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    medication_safety: Mapped[str] = mapped_column(String(32), nullable=False, default="clear")
    fraud_signal: Mapped[str] = mapped_column(String(32), nullable=False, default="clear")
    differential_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actions_taken_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ehr_pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class EhrPatientLink(Base):
    """Map Kareconnect user to external EHR patient identifier."""

    __tablename__ = "ehr_patient_links"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    external_patient_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    external_mrn: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_synced_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class PatientNextOfKin(Base):
    __tablename__ = "patient_next_of_kin"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    relationship: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notify_on_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class GlucoseReading(Base):
    __tablename__ = "glucose_readings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    value_mg_dl: Mapped[float] = mapped_column(Float, nullable=False)
    reading_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="ocr")
    attachment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    ocr_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class MedicationAdjustmentDraft(Base):
    __tablename__ = "medication_adjustment_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    assessment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    triage_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    trend_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_regimen_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_recommendations_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trend_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_clinician", index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class ClinicalReminder(Base):
    __tablename__ = "clinical_reminders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reminder_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    due_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled", index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class EhrSyncLog(Base):
    __tablename__ = "ehr_sync_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # push|fetch
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # ok|failed|skipped
    request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

