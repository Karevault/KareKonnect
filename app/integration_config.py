from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import settings as env_settings
from app.database import session_scope
from app.models import IntegrationSettings
from app.util import now_iso


@dataclass(frozen=True)
class EffectiveIntegration:
    hc_base_url: str
    hc_whatsapp_number: Optional[str]
    hc_sms_join_number: Optional[str]
    meta_wa_phone_number_id: Optional[str]
    meta_wa_waba_id: Optional[str]
    meta_wa_access_token: Optional[str]
    meta_wa_verify_token: Optional[str]
    meta_app_secret: Optional[str]
    meta_graph_version: str
    meta_wa_broadcast_template: Optional[str]
    meta_wa_broadcast_template_language: Optional[str]
    twilio_account_sid: Optional[str]
    twilio_auth_token: Optional[str]
    twilio_messaging_service_sid: Optional[str]
    at_username: Optional[str]
    at_api_key: Optional[str]
    at_shortcode: Optional[str]
    operator_webhook_url: Optional[str]
    helpdesk_phone_e164: Optional[str]
    on_call_clinician_phone_e164: Optional[str]
    paystack_secret_key: Optional[str]
    paystack_public_key: Optional[str]
    meta_wa_calling_enabled: bool
    meta_wa_calling_webrtc_bridge_url: Optional[str]


def _coalesce(db_val: Optional[str], env_val: Optional[str], *, default: Optional[str] = None) -> Optional[str]:
    if db_val is not None and str(db_val).strip() != "":
        return str(db_val).strip()
    if env_val is not None and str(env_val).strip() != "":
        return str(env_val).strip()
    return default


def ensure_integration_settings() -> None:
    with session_scope() as s:
        if s.get(IntegrationSettings, 1):
            return
        s.add(
            IntegrationSettings(
                id=1,
                meta_graph_version="v25.0",
                updated_at=now_iso(),
            )
        )


def get_effective_integration() -> EffectiveIntegration:
    ensure_integration_settings()
    with session_scope() as s:
        row = s.get(IntegrationSettings, 1)
        if row:
            s.expunge(row)
    if row is None:
        raise RuntimeError("integration_settings row missing")

    base = _coalesce(row.hc_base_url, env_settings.base_url) or "http://localhost:8000"
    gv = _coalesce(row.meta_graph_version, env_settings.meta_graph_version, default="v25.0") or "v25.0"

    return EffectiveIntegration(
        hc_base_url=base,
        hc_whatsapp_number=_coalesce(row.hc_whatsapp_number, env_settings.whatsapp_business_number),
        hc_sms_join_number=_coalesce(row.hc_sms_join_number, env_settings.hc_sms_join_number),
        meta_wa_phone_number_id=_coalesce(row.meta_wa_phone_number_id, env_settings.meta_wa_phone_number_id),
        meta_wa_waba_id=_coalesce(row.meta_wa_waba_id, env_settings.meta_wa_waba_id),
        meta_wa_access_token=_coalesce(row.meta_wa_access_token, env_settings.meta_wa_access_token),
        meta_wa_verify_token=_coalesce(row.meta_wa_verify_token, env_settings.meta_wa_verify_token),
        meta_app_secret=_coalesce(row.meta_app_secret, env_settings.meta_app_secret),
        meta_graph_version=gv,
        meta_wa_broadcast_template=_coalesce(
            row.meta_wa_broadcast_template, env_settings.meta_wa_broadcast_template
        ),
        meta_wa_broadcast_template_language=_coalesce(
            row.meta_wa_broadcast_template_language,
            env_settings.meta_wa_broadcast_template_language,
            default="en_US",
        ),
        twilio_account_sid=_coalesce(row.twilio_account_sid, env_settings.twilio_account_sid),
        twilio_auth_token=_coalesce(row.twilio_auth_token, env_settings.twilio_auth_token),
        twilio_messaging_service_sid=_coalesce(
            row.twilio_messaging_service_sid, env_settings.twilio_messaging_service_sid
        ),
        at_username=_coalesce(row.at_username, env_settings.at_username),
        at_api_key=_coalesce(row.at_api_key, env_settings.at_api_key),
        at_shortcode=_coalesce(row.at_shortcode, env_settings.at_shortcode),
        operator_webhook_url=_coalesce(row.operator_webhook_url, env_settings.operator_webhook_url),
        helpdesk_phone_e164=_coalesce(row.helpdesk_phone_e164, env_settings.helpdesk_phone_e164),
        on_call_clinician_phone_e164=_coalesce(
            row.on_call_clinician_phone_e164, env_settings.on_call_clinician_phone_e164
        ),
        paystack_secret_key=_coalesce(row.paystack_secret_key, env_settings.paystack_secret_key),
        paystack_public_key=_coalesce(row.paystack_public_key, env_settings.paystack_public_key),
        meta_wa_calling_enabled=bool(
            row.meta_wa_calling_enabled
            if row.meta_wa_calling_enabled is not None
            else env_settings.meta_wa_calling_enabled
        ),
        meta_wa_calling_webrtc_bridge_url=_coalesce(
            row.meta_wa_calling_webrtc_bridge_url, env_settings.meta_wa_calling_webrtc_bridge_url
        ),
    )
