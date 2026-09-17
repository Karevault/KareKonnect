import os
from dataclasses import dataclass
from typing import Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (_env(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    base_url: str = _env("HC_BASE_URL", "http://localhost:8000") or "http://localhost:8000"
    database_url: str = (
        _env("DATABASE_URL", "postgresql+psycopg2://healthconnect:healthconnect@localhost:5432/healthconnect")
        or "postgresql+psycopg2://healthconnect:healthconnect@localhost:5432/healthconnect"
    )
    default_timezone: str = _env("HC_DEFAULT_TIMEZONE", "Africa/Lagos") or "Africa/Lagos"

    twilio_account_sid: Optional[str] = _env("TWILIO_ACCOUNT_SID")
    twilio_auth_token: Optional[str] = _env("TWILIO_AUTH_TOKEN")
    twilio_messaging_service_sid: Optional[str] = _env("TWILIO_MESSAGING_SERVICE_SID")

    # Meta WhatsApp Cloud API — https://developers.facebook.com/docs/whatsapp/cloud-api
    meta_wa_phone_number_id: Optional[str] = _env("META_WA_PHONE_NUMBER_ID")
    meta_wa_waba_id: Optional[str] = _env("META_WA_WABA_ID")
    meta_wa_access_token: Optional[str] = _env("META_WA_ACCESS_TOKEN")
    meta_app_secret: Optional[str] = _env("META_APP_SECRET")
    meta_wa_verify_token: Optional[str] = _env("META_WA_VERIFY_TOKEN")
    meta_graph_version: str = _env("META_GRAPH_VERSION", "v25.0") or "v25.0"
    meta_wa_broadcast_template: Optional[str] = _env("META_WA_BROADCAST_TEMPLATE")
    meta_wa_broadcast_template_language: Optional[str] = _env("META_WA_BROADCAST_TEMPLATE_LANG", "en_US")

    # Inbound SMS number to show on /join (E.164 or digits). Twilio: the number on your Messaging Service
    # that receives SMS and points to POST {HC_BASE_URL}/webhooks/twilio/inbound
    hc_sms_join_number: Optional[str] = _env("HC_SMS_JOIN_NUMBER")

    operator_webhook_url: Optional[str] = _env("HC_OPERATOR_WEBHOOK_URL")
    # Helpdesk staff E.164 (or digits): receives SMS/WA ticket alerts on the same channel as the patient
    helpdesk_phone_e164: Optional[str] = _env("HC_HELPDESK_PHONE")
    on_call_clinician_phone_e164: Optional[str] = _env("HC_ON_CALL_CLINICIAN_PHONE")

    opay_secret_key: Optional[str] = _env("OPAY_SECRET_KEY")
    opay_public_key: Optional[str] = _env("OPAY_PUBLIC_KEY")
    opay_merchant_id: Optional[str] = _env("OPAY_MERCHANT_ID")
    meta_wa_calling_enabled: bool = _env_bool("HC_META_WA_CALLING_ENABLED", False)
    meta_wa_calling_webrtc_bridge_url: Optional[str] = _env("HC_META_WA_CALLING_WEBRTC_BRIDGE_URL")

    hc_ehr_api_base_url: Optional[str] = _env("HC_EHR_API_BASE_URL")
    hc_ehr_api_key: Optional[str] = _env("HC_EHR_API_KEY")
    hc_ehr_partner_api_key: Optional[str] = _env("HC_EHR_PARTNER_API_KEY")
    hc_ehr_teleconsult_api_key: Optional[str] = _env("HC_EHR_TELECONSULT_API_KEY")
    hc_clinical_intelligence_enabled: bool = _env_bool("HC_CLINICAL_INTELLIGENCE_ENABLED", True)
    hc_triage_window_hours: int = int((_env("HC_TRIAGE_WINDOW_HOURS", "72") or "72").split(".")[0])
    hc_attachments_dir: str = _env("HC_ATTACHMENTS_DIR", "data/attachments") or "data/attachments"

    # WhatsApp Business number for "Chat on WhatsApp" links (digits only, no +), e.g. 2348012345678
    whatsapp_business_number: Optional[str] = _env("HC_WHATSAPP_NUMBER")

    # Africa's Talking — SMS (https://developers.africastalking.com/docs/sms/overview)
    at_username: Optional[str] = _env("AT_USERNAME")
    at_api_key: Optional[str] = _env("AT_API_KEY")
    # Short code / sender ID approved on your AT account (outbound "from")
    at_shortcode: Optional[str] = _env("AT_SHORTCODE")

    # Used by SQLAdmin auth (signs the admin session cookie on /admin only).
    admin_session_secret: str = (
        _env("HC_ADMIN_SESSION_SECRET") or _env("HC_SESSION_SECRET", "change-me") or "change-me"
    )

    # Google Cloud Translation API (REST v2). When set, non-English patient messages are translated
    # to English for FAQ/KareVault matching, and English replies are translated to the user's LANG.
    # Create an API key with "Cloud Translation API" enabled: https://console.cloud.google.com/apis/library/translate.googleapis.com
    google_translate_api_key: Optional[str] = _env("GOOGLE_TRANSLATE_API_KEY")
    # Optional shared key for Speech-to-Text + Text-to-Speech (falls back to GOOGLE_TRANSLATE_API_KEY).
    google_cloud_api_key: Optional[str] = _env("GOOGLE_CLOUD_API_KEY")

    # WhatsApp voice notes (Meta media + Google STT/TTS). Admin toggles override via app_settings row.
    whatsapp_voice_inbound_enabled: bool = _env_bool("HC_WHATSAPP_VOICE_INBOUND", True)
    whatsapp_voice_outbound_enabled: bool = _env_bool("HC_WHATSAPP_VOICE_OUTBOUND", False)
    whatsapp_voice_outbound_max_chars: int = int(
        (_env("HC_WHATSAPP_VOICE_MAX_CHARS", "400") or "400").split(".")[0]
    )

    # First-party translation ML service (optional). When HC_TRANSLATION_ML_BASE_URL is set, the app
    # calls it before Google; results are stored in translation_cache with provider=ml. Bump
    # HC_TRANSLATION_ML_MODEL_VERSION to invalidate cached ML rows when you deploy a new model.
    # Use the same HTTPS origin as HC_BASE_URL to hit /v1/detect and /v1/translate on this app (see ml_translation_router).
    hc_translation_ml_base_url: Optional[str] = _env("HC_TRANSLATION_ML_BASE_URL")
    hc_translation_ml_api_key: Optional[str] = _env("HC_TRANSLATION_ML_API_KEY")
    hc_translation_ml_model_version: Optional[str] = _env("HC_TRANSLATION_ML_MODEL_VERSION", "v2")
    hc_translation_ml_min_confidence: float = _env_float("HC_TRANSLATION_ML_MIN_CONFIDENCE", 0.45)
    # Append-only training rows for your ML pipeline (export / retrain). Default on; set to 0 to disable.
    hc_translation_record_learning: bool = _env_bool("HC_TRANSLATION_RECORD_LEARNING", True)


settings = Settings()

