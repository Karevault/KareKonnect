from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from typing import Optional

import requests

from app import meta_whatsapp as meta_wa
from app.africastalking import at_sms_ready
from app.integration_config import get_effective_integration


@dataclass(frozen=True)
class SelfTestOutcome:
    ok: bool
    title: str
    lines: list[str]


def _meta_graph_version() -> str:
    c = get_effective_integration()
    v = (c.meta_graph_version or "v25.0").strip()
    if not v.startswith("v"):
        return f"v{v}"
    return v


def test_meta_whatsapp() -> SelfTestOutcome:
    title = "Meta WhatsApp (Cloud API)"
    lines: list[str] = []
    if not meta_wa.is_configured():
        return SelfTestOutcome(
            False,
            title,
            ["Phone number ID and access token are not both set (admin or env)."],
        )
    cfg = get_effective_integration()
    pid = (cfg.meta_wa_phone_number_id or "").strip()
    tok = (cfg.meta_wa_access_token or "").strip()
    url = f"https://graph.facebook.com/{_meta_graph_version()}/{pid}"
    params = {"fields": "display_phone_number,verified_name"}
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {tok}"},
            timeout=20,
        )
    except requests.RequestException as e:
        return SelfTestOutcome(False, title, [f"Graph request failed: {e}"])

    if not (200 <= resp.status_code < 300):
        return SelfTestOutcome(
            False,
            title,
            [
                f"Graph HTTP {resp.status_code}",
                (resp.text or "")[:600],
            ],
        )
    data = resp.json() or {}
    disp = data.get("display_phone_number") or "—"
    ver = data.get("verified_name") or "—"
    lines.append("Graph API accepted credentials and phone number ID.")
    lines.append(f"display_phone_number: {disp}")
    lines.append(f"verified_name: {ver}")

    vt = (cfg.meta_wa_verify_token or "").strip()
    if vt:
        lines.append("Webhook verify token is set (check it matches Meta console).")
    else:
        lines.append("WARNING: webhook verify token is empty — Meta GET verification will fail.")

    sec = (cfg.meta_app_secret or "").strip()
    if sec:
        lines.append("App secret is set — inbound POST signatures will be validated.")
    else:
        lines.append("NOTE: app secret empty — signature check is skipped (dev only).")

    lines.append("")
    lines.append("Meta test numbers: developer/test tools are for development; production traffic uses your verified business line once the app is live and compliant.")
    lines.append("Do not treat sandbox test user numbers as your production audience — complete Business Verification and use your approved WABA number for real patients.")

    return SelfTestOutcome(True, title, lines)


def test_broadcast_template() -> SelfTestOutcome:
    title = "WhatsApp broadcast template"
    cfg = get_effective_integration()
    name = (cfg.meta_wa_broadcast_template or "").strip()
    lang = (cfg.meta_wa_broadcast_template_language or "en_US").strip()
    if not name:
        return SelfTestOutcome(
            False,
            title,
            [
                "No broadcast template configured.",
                "Set Meta broadcast template in Deployment & integrations (utility template with {{1}} body).",
            ],
        )
    if not meta_wa.is_configured():
        return SelfTestOutcome(False, title, ["Meta WhatsApp is not configured."])

    lines = [f"Template name: {name}", f"Language: {lang}"]
    waba = (cfg.meta_wa_waba_id or "").strip()

    if waba:
        templates, err = meta_wa.list_message_templates(waba_id=waba)
        if err:
            lines.append(f"Could not list templates for WABA {waba}: {err}")
        elif templates:
            matches = [
                t
                for t in templates
                if (t.get("name") or "").lower() == name.lower()
                and (t.get("status") or "").upper() == "APPROVED"
            ]
            if matches:
                for t in matches:
                    lines.append(
                        f"Approved on WABA: {t.get('name')} lang={t.get('language')} category={t.get('category')}"
                    )
                return SelfTestOutcome(True, title, lines)
            lines.append(f"Template {name!r} not found (approved) on WABA {waba}. Available:")
            for t in templates[:15]:
                lines.append(f"  • {t.get('name')} ({t.get('language')}, {t.get('status')})")
            return SelfTestOutcome(False, title, lines)
        else:
            lines.append(f"No templates returned for WABA {waba}.")
    else:
        lines.append("Set Meta WABA ID to list approved templates from WhatsApp Manager.")

    ok, err = meta_wa.send_template_message(
        to_phone_e164="+2348055749136",
        template_name=name,
        language_code=lang,
        body_parameters=["Kareconnect template validation probe"],
    )
    if ok:
        lines.append("Probe send accepted by Meta (template registered on this phone number).")
        return SelfTestOutcome(True, title, lines)
    if err and "132001" in err:
        lines.append(
            f"FAILED: Meta error 132001 — template {name!r} is not on this WhatsApp business line "
            f"(languages tried: {', '.join(meta_wa._template_language_candidates(lang))})."
        )
        lines.append(
            "Create or approve the template in WhatsApp Manager for HealthConnect (+234 816 287 0026). "
            "Copy the exact template name and use language English (US) = en_US."
        )
        return SelfTestOutcome(False, title, lines)
    if err:
        lines.append(f"Meta response: {err[:400]}")
    return SelfTestOutcome(False, title, lines)


def test_paystack() -> SelfTestOutcome:
    from app.paystack_billing import test_paystack_initialize

    title = "Paystack (telehealth payments)"
    ok, detail = test_paystack_initialize()
    return SelfTestOutcome(ok, title, [detail])


def test_twilio() -> SelfTestOutcome:
    title = "Twilio (Account + Messaging Service)"
    cfg = get_effective_integration()
    sid = (cfg.twilio_account_sid or "").strip()
    tok = (cfg.twilio_auth_token or "").strip()
    msid = (cfg.twilio_messaging_service_sid or "").strip()
    if not (sid and tok and msid):
        return SelfTestOutcome(
            False,
            title,
            [
                "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_MESSAGING_SERVICE_SID (or admin fields).",
            ],
        )
    auth = base64.b64encode(f"{sid}:{tok}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    lines: list[str] = []

    try:
        r1 = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as e:
        return SelfTestOutcome(False, title, [f"Account request failed: {e}"])

    if not (200 <= r1.status_code < 300):
        return SelfTestOutcome(
            False,
            title,
            [f"Account HTTP {r1.status_code}", (r1.text or "")[:500]],
        )
    acc = r1.json() or {}
    lines.append(f"Account OK ({acc.get('friendly_name') or sid}).")

    try:
        r2 = requests.get(
            f"https://messaging.twilio.com/v1/Services/{msid}",
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as e:
        return SelfTestOutcome(False, title, lines + [f"Messaging Service request failed: {e}"])

    if not (200 <= r2.status_code < 300):
        return SelfTestOutcome(
            False,
            title,
            lines + [f"Messaging Service HTTP {r2.status_code}", (r2.text or "")[:500]],
        )
    svc = r2.json() or {}
    lines.append(f"Messaging Service OK ({svc.get('friendly_name') or msid}).")
    lines.append("")
    lines.append("Inbound SMS/WhatsApp (Twilio): Phone number → Messaging → “A message comes in” → Webhook POST your public URL + /webhooks/twilio/inbound")
    lines.append("If you use a Messaging Service, add senders under the service and set inbound on the number or service per Twilio docs.")

    return SelfTestOutcome(True, title, lines)


def test_africastalking() -> SelfTestOutcome:
    title = "Africa's Talking (SMS credentials)"
    if not at_sms_ready():
        return SelfTestOutcome(
            False,
            title,
            ["Set AT_USERNAME, AT_API_KEY, and AT_SHORTCODE (or admin fields)."],
        )
    cfg = get_effective_integration()
    user = (cfg.at_username or "").strip()
    key = (cfg.at_api_key or "").strip()
    url = "https://api.africastalking.com/version1/user"
    headers = {"Accept": "application/json", "apiKey": key}
    try:
        resp = requests.get(url, params={"username": user}, headers=headers, timeout=20)
    except requests.RequestException as e:
        return SelfTestOutcome(False, title, [f"User API request failed: {e}"])

    if not (200 <= resp.status_code < 300):
        return SelfTestOutcome(
            False,
            title,
            [f"User API HTTP {resp.status_code}", (resp.text or "")[:500]],
        )
    base = (cfg.hc_base_url or "").rstrip("/") or "(set HC_BASE_URL)"
    lines = [
        "User API accepted apiKey + username.",
        f"Configured sender / shortcode field: {(cfg.at_shortcode or '').strip() or '—'}",
        f"Inbound SMS callback should POST to {base}/webhooks/africastalking/sms (see AT dashboard).",
    ]
    return SelfTestOutcome(True, title, lines)


def format_outcome_html(outcome: SelfTestOutcome, *, back_url: str) -> str:
    status = "Success" if outcome.ok else "Failed"
    color = "#047857" if outcome.ok else "#b91c1c"
    body = "\n".join(f"<p>{html.escape(line)}</p>" for line in outcome.lines)
    safe_back = html.escape(back_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{html.escape(outcome.title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}</style>
</head><body>
<h1>{html.escape(outcome.title)}</h1>
<p><strong style="color:{color}">{status}</strong></p>
{body}
<p style="margin-top:2rem"><a href="{safe_back}">← Back to integration settings</a></p>
</body></html>"""


def safe_back_url(referer: Optional[str]) -> str:
    if referer and "/integration-settings/" in referer:
        return referer
    return "/admin/integration-settings/edit/1"
