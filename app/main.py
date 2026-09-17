from __future__ import annotations

import io
import json
import logging
import os
import threading
import uuid
from datetime import date
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from typing import Optional
from urllib.parse import quote

import anyio

import qrcode
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from app.admin import mount_admin
from app import meta_whatsapp as meta_wa
from app.africastalking import (
    at_sms_ready,
    normalize_at_phone_number,
    parse_incoming_sms_form,
    parse_ussd_form,
    send_sms as at_send_sms,
)
from app.config import settings
from app.db import init_db
from app.integration_config import ensure_integration_settings, get_effective_integration
from app.inbound_chat import handle_inbound_text_message
from app.messaging import send_message
from app.whatsapp_voice import (
    inbound_voice_enabled,
    send_whatsapp_patient_reply,
    transcribe_inbound_audio,
)
from app.twilio_delivery import record_twilio_status_form
from app.services import (
    enroll_programs,
    ensure_app_settings,
    get_app_settings,
    request_appointment,
    request_escalation,
    request_rpm,
    send_queued_messages,
    enqueue_due_messages,
)
from app.ussd_flow import run_ussd_step
from app.ml_translation_router import router as ml_translation_router
from app.neural_marian import start_marian_warmup_background
from app.translation import translate_outbound_for_user
from app.util import format_help_keywords_natural_list, format_datetime_human, install_jinja_datetime_filters, normalize_phone_e164


app = FastAPI(title="Kareconnect", version="0.1.0")
app.include_router(ml_translation_router)
templates = Jinja2Templates(directory="app/templates")
install_jinja_datetime_filters(templates.env)
_test_chat_lock = threading.Lock()
_test_chat_sessions: dict[str, dict[str, object]] = {}

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
_admin_static_dir = Path(__file__).resolve().parent / "static" / "admin"
_public_dir = Path(__file__).resolve().parent.parent / "public"
app.mount(
    "/admin-hc",
    StaticFiles(directory=str(_admin_static_dir)),
    name="admin_hc_static",
)
app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public_assets")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(_public_dir / "favicon.png", media_type="image/png")


def _warn_if_no_admin_users() -> None:
    import logging

    from sqlalchemy import func, select

    from app.database import session_scope
    from app.models import AdminUser

    with session_scope() as s:
        n = int(s.execute(select(func.count()).select_from(AdminUser)).scalar_one() or 0)
    if n == 0:
        logging.getLogger("uvicorn.error").warning(
            "No admin_users found. Create the first admin with: "
            "./scripts/create_admin.sh <username> '<password>'"
        )


def _twiml(message: str) -> str:
    # Minimal TwiML for SMS/WhatsApp (Twilio ignores empty <Message> — give a fallback)
    m = (message or "").strip()
    if not m:
        m = "We received your message. Reply INFO or MENU for options, JOIN to enrol from this chat, STOP to pause."
    m = (
        m.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{m}</Message></Response>'


def _infer_channel(from_value: str) -> Literal["whatsapp", "sms"]:
    return "whatsapp" if (from_value or "").startswith("whatsapp:") else "sms"


_INBOUND_ROUTING_TIMEOUT_SEC = float(
    __import__("os").getenv("HC_INBOUND_ROUTING_TIMEOUT_SEC", "28")
)


def _inbound_fallback_text() -> str:
    cfg = get_app_settings()
    if cfg.auto_reply_enabled and (cfg.unknown_fallback_text or "").strip():
        return cfg.unknown_fallback_text.strip()
    return "We received your message. Reply INFO or MENU for keywords."


async def _handle_inbound_async(
    *,
    channel: Literal["sms", "whatsapp"],
    from_phone_raw: str,
    to_phone_raw: Optional[str],
    body: str,
    raw: dict,
) -> str:
    """Run sync routing off the event loop so Marian load does not block other requests."""
    try:
        with anyio.fail_after(_INBOUND_ROUTING_TIMEOUT_SEC):
            return await anyio.to_thread.run_sync(
                partial(
                    handle_inbound_text_message,
                    channel=channel,
                    from_phone_raw=from_phone_raw,
                    to_phone_raw=to_phone_raw,
                    body=body,
                    raw=raw,
                ),
            )
    except TimeoutError:
        logging.getLogger("uvicorn.error").warning(
            "Inbound routing timed out after %ss (often first Pidgin model load)",
            _INBOUND_ROUTING_TIMEOUT_SEC,
        )
        return _inbound_fallback_text()


def _deliver_stale_broadcasts() -> None:
    """Retry any broadcast outbox rows left queued (e.g. admin flush timed out)."""
    from sqlalchemy import select

    from app.broadcast import deliver_broadcast_run
    from app.database import session_scope
    from app.models import MessageOutbox

    with session_scope() as s:
        run_ids = list(
            s.execute(
                select(MessageOutbox.broadcast_run_id)
                .where(
                    MessageOutbox.broadcast_run_id.isnot(None),
                    MessageOutbox.status == "queued",
                )
                .distinct()
            )
            .scalars()
            .all()
        )
    for rid in run_ids:
        if rid:
            deliver_broadcast_run(run_id=rid)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    ensure_app_settings()
    ensure_integration_settings()
    _warn_if_no_admin_users()
    mount_admin(app)
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(enqueue_due_messages, "interval", minutes=1, id="enqueue_due_messages")
    sched.add_job(_deliver_stale_broadcasts, "interval", minutes=1, id="deliver_stale_broadcasts")
    sched.add_job(send_queued_messages, "interval", minutes=1, id="send_queued_messages")
    sched.start()
    start_marian_warmup_background()


def _whatsapp_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    return digits or None


def _whatsapp_join_link_and_keyword() -> tuple[Optional[str], str]:
    """wa.me deep link (scan / tap opens WhatsApp with join keyword) and the keyword shown in copy."""
    cfg = get_app_settings()
    join_kw = (cfg.whatsapp_join_keywords.split(",")[0].strip() or "JOIN").upper()
    integ = get_effective_integration()
    wa_digits: Optional[str] = None
    if meta_wa.is_configured():
        wa_digits = meta_wa.get_wa_me_digits_from_meta(cfg=integ)
    if not wa_digits:
        wa_digits = _whatsapp_digits(integ.hc_whatsapp_number)
    link = f"https://wa.me/{wa_digits}?text={quote(join_kw)}" if wa_digits else None
    return link, join_kw


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy(request: Request):
    """Public privacy policy for Meta / app stores and enrolment transparency."""
    base = get_effective_integration().hc_base_url.rstrip("/")
    return templates.TemplateResponse(
        "privacy.html",
        {
            "request": request,
            "base_url": base,
            "effective_date": "20 April 2026",
        },
    )


@app.get("/privacy-policy")
def privacy_policy_alias():
    return RedirectResponse(url="/privacy", status_code=301)


@app.get("/", response_class=HTMLResponse)
@app.get("/join", response_class=HTMLResponse)
def join_page(
    request: Request,
    success: Optional[str] = None,
    error: Optional[str] = None,
):
    cfg = get_app_settings()
    sms_join_kw = (cfg.sms_join_keywords.split(",")[0].strip() or "JOIN").upper()
    integ = get_effective_integration()
    wa_link, join_kw = _whatsapp_join_link_and_keyword()
    sms_num = (integ.hc_sms_join_number or "").strip() or None
    sms_help_hint = format_help_keywords_natural_list(cfg.sms_help_keywords)
    wa_help_hint = format_help_keywords_natural_list(cfg.whatsapp_help_keywords)
    symptom_check_hint = format_help_keywords_natural_list(cfg.symptom_check_keywords)
    return templates.TemplateResponse(
        "join.html",
        {
            "request": request,
            "success": success,
            "error": error,
            "wa_link": wa_link,
            "wa_join_keyword": join_kw,
            "sms_join_number": sms_num,
            "sms_join_keyword": sms_join_kw,
            "sms_help_hint": sms_help_hint,
            "wa_help_hint": wa_help_hint,
            "symptom_check_hint": symptom_check_hint,
        },
    )


@app.post("/join", response_class=HTMLResponse)
def join_submit(
    request: Request,
    full_name: Annotated[str, Form(...)],
    phone_e164: Annotated[str, Form(...)],
    channel: Annotated[Literal["sms", "whatsapp"], Form(...)],
    language: Annotated[Literal["en", "pcm", "ha", "ig", "yo"], Form(...)],
    programs: Annotated[Optional[list[str]], Form()] = None,
    edd: Annotated[Optional[str], Form()] = None,
    recurring_whatsapp_opt_in: Annotated[Optional[str], Form()] = None,
):
    programs = programs or []
    name = " ".join((full_name or "").strip().split())
    if not name:
        return join_page(request, error="Please enter your full name.")
    phone = normalize_phone_e164(phone_e164)
    if not phone.startswith("+"):
        return join_page(request, error="Please use E.164 format like +2348012345678.")
    if not programs:
        return join_page(request, error="Please select at least one program.")
    if "maternal_child" in programs and edd:
        try:
            date.fromisoformat(edd)
        except ValueError:
            return join_page(request, error="EDD must be a valid date.")

    recurring_kw: Optional[bool] = None
    if channel == "whatsapp":
        recurring_kw = (recurring_whatsapp_opt_in or "").strip().lower() in ("1", "on", "yes", "true")

    user_id, newly_enrolled = enroll_programs(
        phone_e164=phone,
        channel=channel,
        language=language,
        programs=[p for p in programs if p in ("maternal_child", "chronic", "youth", "kidney")],
        edd_iso=edd,
        full_name=name,
        recurring_messages_opt_in=recurring_kw,
    )
    if not newly_enrolled:
        return join_page(
            request,
            success=f"This phone number is already enrolled (user {user_id}). No duplicate registration was created.",
        )
    cfg = get_app_settings()
    hk = (
        format_help_keywords_natural_list(cfg.whatsapp_help_keywords)
        if channel == "whatsapp"
        else format_help_keywords_natural_list(cfg.sms_help_keywords)
    )
    sc = format_help_keywords_natural_list(cfg.symptom_check_keywords)
    from app.triage import start_triage_session, triage_welcome_addon

    if channel == "whatsapp":
        start_triage_session(phone_e164=phone, channel="whatsapp", user_id=user_id)
    hint = (
        f"Kareconnect: you're enrolled. Programmes: maternal & child, chronic, youth, kidney. "
        "Health questions: reply in your own words. "
        "Our team: start a message with Helpdesk. "
        f"Send {hk} for all commands. "
        f"Send {sc} for a short symptom-checker reminder. "
        "Send JOIN to repeat the full welcome."
    )
    if channel == "whatsapp":
        hint += triage_welcome_addon()
    hint_out = translate_outbound_for_user(hint, language)
    hint_res = send_message(to_phone_e164=phone, body=hint_out, channel=channel)
    if not hint_res.ok:
        logging.getLogger("uvicorn.error").warning("Post-join hint send failed: %s", hint_res.error)
    tip_note = ""
    if channel == "whatsapp":
        tip_note = (
            " WhatsApp programme tips are on (at most one education message per day)."
            if recurring_kw
            else " Optional WhatsApp programme tips: send REMINDERS YES in chat to opt in."
        )
    return join_page(
        request,
        success=f"Joined successfully (user {user_id}).{tip_note}",
    )


def _get_or_create_test_chat_session(session_id: Optional[str] = None) -> tuple[str, dict[str, object]]:
    with _test_chat_lock:
        if session_id and session_id in _test_chat_sessions:
            return session_id, _test_chat_sessions[session_id]
        sid = f"tc_{uuid.uuid4().hex[:12]}"
        state: dict[str, object] = {
            "channel": "whatsapp",
            "phone_e164": "+2348010000000",
            "to_phone_e164": "+14155238886",
            "messages": [],
        }
        _test_chat_sessions[sid] = state
        return sid, state


def _reset_test_chat_state_for_identity(
    state: dict[str, object],
    *,
    channel: Literal["sms", "whatsapp"],
    phone_e164: str,
    to_phone_e164: str,
) -> bool:
    prev_channel = str(state.get("channel") or "")
    prev_phone = normalize_phone_e164(str(state.get("phone_e164") or ""))
    prev_to_phone = normalize_phone_e164(str(state.get("to_phone_e164") or ""))
    changed = (prev_channel, prev_phone, prev_to_phone) != (channel, phone_e164, to_phone_e164)

    state["channel"] = channel
    state["phone_e164"] = phone_e164
    state["to_phone_e164"] = to_phone_e164
    if changed:
        state["messages"] = []
    else:
        state.setdefault("messages", [])
    return changed


@app.get("/test/chat", response_class=HTMLResponse)
def test_chat_page(request: Request, session_id: Optional[str] = None):
    sid, state = _get_or_create_test_chat_session(session_id)
    return templates.TemplateResponse(
        "test_chat.html",
        {
            "request": request,
            "session_id": sid,
            "state": state,
            "error": None,
        },
    )


@app.post("/test/chat", response_class=HTMLResponse)
async def test_chat_send(
    request: Request,
    session_id: Annotated[str, Form(...)],
    channel: Annotated[Literal["sms", "whatsapp"], Form(...)],
    phone_e164: Annotated[str, Form(...)],
    to_phone_e164: Annotated[str, Form(...)],
    body: Annotated[str, Form(...)],
):
    sid, state = _get_or_create_test_chat_session(session_id)
    msg = (body or "").strip()
    phone = normalize_phone_e164(phone_e164)
    to_phone = normalize_phone_e164(to_phone_e164)
    if not msg:
        return templates.TemplateResponse(
            "test_chat.html",
            {"request": request, "session_id": sid, "state": state, "error": "Type a message to send."},
        )
    with _test_chat_lock:
        identity_changed = _reset_test_chat_state_for_identity(
            state,
            channel=channel,
            phone_e164=phone,
            to_phone_e164=to_phone,
        )
        messages = state.setdefault("messages", [])
        assert isinstance(messages, list)
        if identity_changed:
            messages.append(
                {
                    "role": "system",
                    "text": "Conversation reset because the simulated phone/channel changed. Previous number state is isolated.",
                }
            )
        messages.append({"role": "user", "text": msg})
    try:
        from_raw = f"whatsapp:{phone}" if channel == "whatsapp" else phone
        to_raw = f"whatsapp:{to_phone}" if channel == "whatsapp" else to_phone
        reply = await _handle_inbound_async(
            channel=channel,
            from_phone_raw=from_raw,
            to_phone_raw=to_raw,
            body=msg,
            raw={"test_chat": "1", "session_id": sid},
        )
    except Exception as exc:
        logging.getLogger("uvicorn.error").exception("test_chat routing failed")
        reply = f"(error) {type(exc).__name__}: {exc}"
    with _test_chat_lock:
        messages = state.setdefault("messages", [])
        assert isinstance(messages, list)
        messages.append({"role": "bot", "text": reply or "(empty reply)"})
        if len(messages) > 80:
            del messages[:-80]
    return templates.TemplateResponse(
        "test_chat.html",
        {"request": request, "session_id": sid, "state": state, "error": None},
    )


@app.get("/join/qr.png")
def join_qr_png() -> Response:
    """PNG QR encoding the public join page (GET /join)."""
    join_url = f"{get_effective_integration().hc_base_url.rstrip('/')}/join"
    img = qrcode.make(join_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/join/wa-qr.png")
def join_wa_qr_png() -> Response:
    """PNG QR encoding wa.me join link (same as Open WhatsApp)."""
    wa_link, _ = _whatsapp_join_link_and_keyword()
    if not wa_link:
        raise HTTPException(status_code=404, detail="WhatsApp join not configured")
    img = qrcode.make(wa_link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/webhooks/meta/whatsapp", response_class=PlainTextResponse)
async def meta_whatsapp_verify(request: Request):
    """
    Meta Cloud API webhook verification (GET).
    https://developers.facebook.com/docs/graph-api/webhooks/getting-started
    """
    if not meta_wa.verify_token_configured():
        raise HTTPException(status_code=503, detail="META_WA_VERIFY_TOKEN is not set")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    echoed = meta_wa.verify_webhook_subscription(mode=mode, token=token, challenge=challenge)
    if echoed is None:
        raise HTTPException(status_code=403, detail="Verification failed")
    return PlainTextResponse(echoed)


@app.post("/webhooks/meta/whatsapp")
async def meta_whatsapp_inbound(request: Request):
    """
    Inbound WhatsApp messages (JSON). Replies are sent via Meta Cloud API when META_WA_* is configured.
    """
    if not meta_wa.is_configured():
        raise HTTPException(status_code=404, detail="Meta WhatsApp not configured")
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not meta_wa.verify_signature_sha256(body, sig):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Hub-Signature-256")
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    log = logging.getLogger("uvicorn.error")
    had_any = False

    for st in meta_wa.iter_message_statuses(payload):
        had_any = True
        if st["status"] in ("failed", "undelivered"):
            log.error(
                "Meta WhatsApp delivery %s for %s (wamid=%s): %s",
                st["status"],
                st["recipient_id"],
                st["message_id"],
                st["error"] or "no detail",
            )
            from app.broadcast import apply_meta_delivery_status

            await anyio.to_thread.run_sync(
                lambda: apply_meta_delivery_status(
                    recipient_digits=st["recipient_id"],
                    status=st["status"],
                    error=st["error"],
                )
            )
        elif st["status"] == "delivered":
            log.info(
                "Meta WhatsApp delivered to %s (wamid=%s)",
                st["recipient_id"],
                st["message_id"],
            )

    from app.media_intake import process_inbound_whatsapp_media

    for ev in meta_wa.iter_inbound_media_messages(payload):
        had_any = True
        to_e164 = normalize_at_phone_number(ev["from_wa_id"])
        try:
            reply = await anyio.to_thread.run_sync(
                lambda e=ev: process_inbound_whatsapp_media(
                    phone_e164=to_e164,
                    media_type=e["media_type"],
                    media_id=e["media_id"],
                    mime_type=e.get("mime_type"),
                    caption=e.get("caption"),
                    filename=e.get("filename"),
                    meta_message_id=e.get("message_id"),
                )
            )
        except Exception:
            log.exception("meta_whatsapp media routing failed")
            reply = "We received your file but could not process it. Please try again with a short caption."
        if reply.strip():
            res = await anyio.to_thread.run_sync(
                partial(
                    send_whatsapp_patient_reply,
                    to_phone_e164=to_e164,
                    body=reply,
                    patient_phone_e164=to_e164,
                    allow_voice=False,
                ),
            )
            if not res.ok:
                log.error("Meta WhatsApp media reply send failed: %s", res.error)

    for ev in meta_wa.iter_inbound_audio_messages(payload):
        had_any = True
        to_e164 = normalize_at_phone_number(ev["from_wa_id"])
        if not inbound_voice_enabled():
            reply = (
                "Voice notes are not enabled on this service yet. "
                "Please type your message, or send INFO for commands."
            )
        else:
            transcript, stt_err = await anyio.to_thread.run_sync(
                transcribe_inbound_audio,
                media_id=ev["media_id"],
                phone_e164=to_e164,
                meta_message_id=ev.get("message_id"),
            )
            if transcript:
                raw = {str(k): str(v) for k, v in ev["raw"].items()}
                raw["voice_transcript"] = transcript
                try:
                    reply = await _handle_inbound_async(
                        channel="whatsapp",
                        from_phone_raw=ev["from_wa_id"],
                        to_phone_raw=ev.get("to_display_phone"),
                        body=transcript,
                        raw=raw,
                    )
                except Exception:
                    log.exception("meta_whatsapp voice routing failed")
                    reply = _inbound_fallback_text()
            else:
                log.warning("WhatsApp voice STT failed: %s", stt_err)
                reply = (
                    "We could not understand your voice note. "
                    "Please type your message or try again. Send INFO for commands."
                )
        if reply.strip():
            res = await anyio.to_thread.run_sync(
                partial(
                    send_whatsapp_patient_reply,
                    to_phone_e164=to_e164,
                    body=reply,
                    patient_phone_e164=to_e164,
                    allow_voice=False,  # STT path; text reply only (webhook must stay fast)
                ),
            )
            if not res.ok:
                log.error("Meta WhatsApp voice reply send failed: %s", res.error)

    from app.meta_whatsapp_calling import iter_call_webhook_events
    from app.telehealth import apply_meta_call_webhook_event

    for call_ev in iter_call_webhook_events(payload):
        had_any = True
        await anyio.to_thread.run_sync(lambda ev=call_ev: apply_meta_call_webhook_event(ev))

    for ev in meta_wa.iter_inbound_text_messages(payload):
        had_any = True
        to_e164 = normalize_at_phone_number(ev["from_wa_id"])
        try:
            reply = await _handle_inbound_async(
                channel="whatsapp",
                from_phone_raw=ev["from_wa_id"],
                to_phone_raw=ev.get("to_display_phone"),
                body=ev["body"],
                raw={str(k): str(v) for k, v in ev["raw"].items()},
            )
        except Exception:
            log.exception("meta_whatsapp inbound routing failed")
            reply = _inbound_fallback_text()
        body_raw = (ev.get("body") or "").strip()
        if reply.strip():
            res = await anyio.to_thread.run_sync(
                partial(
                    send_whatsapp_patient_reply,
                    to_phone_e164=to_e164,
                    body=reply,
                    patient_phone_e164=to_e164,
                    allow_voice=False,  # text-only in webhook for reliable Meta delivery
                ),
            )
            if not res.ok:
                log.error("Meta WhatsApp reply send failed: %s", res.error)
        elif body_raw:
            cfg_app = get_app_settings()
            fb = (
                cfg_app.unknown_fallback_text
                if cfg_app.auto_reply_enabled
                else "We received your message. Reply INFO or MENU for keywords, JOIN to enrol from this chat, STOP to pause."
            )
            res = await anyio.to_thread.run_sync(
                partial(
                    send_whatsapp_patient_reply,
                    to_phone_e164=to_e164,
                    body=fb,
                    patient_phone_e164=to_e164,
                    allow_voice=False,
                ),
            )
            if not res.ok:
                log.error("Meta WhatsApp fallback send failed: %s", res.error)

    if not had_any:
        log.warning(
            "Meta WhatsApp webhook: no processable inbound text/audio messages in payload "
            "(subscribe webhook field **messages**, or payload may be delivery/status only)."
        )

    send_queued_messages()

    return JSONResponse({"status": "ok"})


@app.get("/telehealth/payment/complete", response_class=HTMLResponse)
def telehealth_payment_complete(
    request: Request,
    session_id: Optional[str] = None,
    reference: Optional[str] = None,
):
    """Browser return after OPay checkout — verifies payment and sends call instructions."""
    import logging

    from app.telehealth import confirm_payment_from_browser_callback

    log = logging.getLogger(__name__)
    try:
        title, body, _sent = confirm_payment_from_browser_callback(
            session_id,
            reference=reference,
        )
    except Exception:
        log.exception(
            "telehealth payment complete failed session_id=%s reference=%s",
            session_id,
            reference,
        )
        title = "Payment processing"
        body = (
            "We received your return from OPay but could not finish automatically. "
            "If payment succeeded, return to your Kareconnect WhatsApp chat — instructions are sent when payment confirms. "
            "You can also refresh this page in a minute."
        )
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;padding:2rem;max-width:36rem;line-height:1.5'>"
        f"<h2>{title}</h2><p>{body}</p>"
        f"</body></html>"
    )


@app.post("/webhooks/opay")
async def opay_webhook(request: Request):
    """
    OPay payment status callback: payload.status == SUCCESS marks the booking paid
    and sends video call instructions.

    Register as the callback URL in the OPay merchant dashboard:
    POST https://kareconnect.karevault.com/webhooks/opay
    """
    from app.opay_billing import verify_webhook_signature
    from app.telehealth import apply_opay_webhook_event

    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not verify_webhook_signature(payload):
        raise HTTPException(status_code=403, detail="Invalid OPay signature")
    await anyio.to_thread.run_sync(lambda: apply_opay_webhook_event(payload))
    return JSONResponse({"status": "ok"})


@app.post("/webhooks/twilio/status", response_class=PlainTextResponse)
async def twilio_message_status(request: Request):
    """
    Twilio delivery status callback (Messaging / SMS).
    Set on your Messaging Service: Integration → Status callback URL, or per-message via StatusCallback (set automatically for HTTPS HC_BASE_URL).
    """
    form = await request.form()
    record_twilio_status_form(dict(form))
    return PlainTextResponse("OK")


@app.post("/webhooks/twilio/inbound", response_class=PlainTextResponse)
async def twilio_inbound(request: Request):
    form = await request.form()
    body = (form.get("Body") or "").strip()
    from_value = str(form.get("From") or "")
    to_value = str(form.get("To") or "")
    channel = _infer_channel(from_value)
    raw = {k: str(v) for k, v in form.items()}
    try:
        reply = await _handle_inbound_async(
            channel=channel,
            from_phone_raw=from_value,
            to_phone_raw=to_value or None,
            body=body,
            raw=raw,
        )
    except Exception:
        logging.getLogger("uvicorn.error").exception("twilio_inbound routing failed")
        reply = _inbound_fallback_text()
    send_queued_messages()
    return PlainTextResponse(_twiml(reply), media_type="application/xml")


@app.post("/webhooks/africastalking/sms", response_class=PlainTextResponse)
async def africastalking_sms_inbound(request: Request):
    """
    Inbound SMS callback (form POST). Replies are sent via Africa's Talking HTTP API when AT_* is configured.
    https://developers.africastalking.com/docs/sms/notifications
    """
    form = await request.form()
    fd = {str(k): str(v) for k, v in form.items()}
    from_n, to_n, text, raw = parse_incoming_sms_form(fd)
    try:
        reply = await _handle_inbound_async(
            channel="sms",
            from_phone_raw=from_n,
            to_phone_raw=to_n or None,
            body=text,
            raw=raw,
        )
    except Exception:
        logging.getLogger("uvicorn.error").exception("africastalking_sms_inbound routing failed")
        cfg = get_app_settings()
        reply = (
            cfg.unknown_fallback_text
            if cfg.auto_reply_enabled
            else "We received your message. Reply INFO or MENU for keywords."
        )
    if reply.strip() and at_sms_ready():
        at_send_sms(to_e164=from_n, message=reply)
    send_queued_messages()
    return PlainTextResponse("OK")


@app.post("/webhooks/africastalking/ussd", response_class=PlainTextResponse)
async def africastalking_ussd(request: Request):
    """
    USSD callback — respond with `CON\\n...` or `END\\n...` as plain text.
    https://developers.africastalking.com/docs/ussd/overview
    """
    form = await request.form()
    fd = {str(k): str(v) for k, v in form.items()}
    _session_id, _service_code, phone, text, _raw = parse_ussd_form(fd)
    action, message = run_ussd_step(phone, text)
    return PlainTextResponse(f"{action}\n{message}", media_type="text/plain")


class UssdSessionRequest(BaseModel):
    session_id: str = Field(..., description="USSD gateway session identifier")
    phone_e164: str
    text: str = Field("", description="User input text from gateway")


class UssdSessionResponse(BaseModel):
    action: Literal["CON", "END"]
    message: str


@app.post("/api/ussd/session", response_model=UssdSessionResponse)
def ussd_session(req: UssdSessionRequest):
    """
    JSON USSD step for gateways that call an HTTP JSON API (same flow as Africa's Talking USSD webhook).
    """
    action, message = run_ussd_step(req.phone_e164, req.text)
    return UssdSessionResponse(action=action, message=message)


class EscalationCreate(BaseModel):
    phone_e164: str
    reason: str
    urgency: Literal["routine", "urgent", "emergency"] = "routine"


@app.post("/api/escalations")
def api_escalations(req: EscalationCreate):
    esc_id = request_escalation(from_phone=req.phone_e164, reason=req.reason, urgency=req.urgency)
    return {"id": esc_id, "status": "requested"}


class AppointmentCreate(BaseModel):
    phone_e164: str
    reason: str
    preferred_date: Optional[str] = None


@app.post("/api/appointments")
def api_appointments(req: AppointmentCreate):
    appt_id = request_appointment(from_phone=req.phone_e164, reason=req.reason, preferred_date=req.preferred_date)
    return {"id": appt_id, "status": "requested"}


class RpmEnrollCreate(BaseModel):
    phone_e164: str
    condition: str
    device_type: Optional[str] = None


@app.post("/api/rpm/enroll")
def api_rpm(req: RpmEnrollCreate):
    rpm_id = request_rpm(from_phone=req.phone_e164, condition=req.condition, device_type=req.device_type)
    return {"id": rpm_id, "status": "requested"}


class EhrLinkCreate(BaseModel):
    user_id: str
    external_patient_id: str
    external_mrn: Optional[str] = None


@app.post("/api/ehr/link")
def api_ehr_link(req: EhrLinkCreate):
    """Link Kareconnect user to your EHR patient record (configure HC_EHR_API_BASE_URL for sync)."""
    from app.ehr_client import link_patient

    link_id = link_patient(
        user_id=req.user_id,
        external_patient_id=req.external_patient_id,
        external_mrn=req.external_mrn,
    )
    return {"id": link_id, "status": "linked"}


class NextOfKinCreate(BaseModel):
    user_id: str
    full_name: str
    phone_e164: str
    relationship: Optional[str] = None
    notify_on_emergency: bool = True


@app.post("/api/patients/next-of-kin")
def api_add_next_of_kin(req: NextOfKinCreate):
    from app.next_of_kin import add_patient_next_of_kin

    nid = add_patient_next_of_kin(
        user_id=req.user_id,
        full_name=req.full_name,
        phone_e164=req.phone_e164,
        relationship=req.relationship,
        notify_on_emergency=req.notify_on_emergency,
    )
    return {"id": nid, "status": "created"}


@app.get("/api/patients/{user_id}/next-of-kin")
def api_list_next_of_kin(user_id: str):
    """Emergency contacts for one patient (each Kareconnect user has their own list)."""
    from app.next_of_kin import list_patient_next_of_kin

    contacts = list_patient_next_of_kin(user_id=user_id)
    return {
        "user_id": user_id,
        "contacts": [
            {
                "full_name": c.full_name,
                "phone_e164": c.phone_e164,
                "relationship": c.relationship,
                "source": c.source,
            }
            for c in contacts
        ],
    }


class EhrEnsurePatientRequest(BaseModel):
    user_id: str
    phone_e164: str
    full_name: Optional[str] = None


@app.post("/api/ehr/ensure-patient")
def api_ehr_ensure_patient(req: EhrEnsurePatientRequest):
    """
    Resolve EHR identity for a Kareconnect user: lookup by phone, create if missing, store link.
    """
    from app.ehr_client import resolve_ehr_patient

    ext_id = resolve_ehr_patient(
        user_id=req.user_id,
        phone_e164=req.phone_e164,
        full_name=req.full_name,
    )
    return {
        "external_patient_id": ext_id,
        "status": "linked" if ext_id else "unconfigured_or_failed",
    }


class EhrPatientContextRequest(BaseModel):
    phone_e164: str


@app.post("/api/ehr/patient-context")
def api_ehr_patient_context(req: EhrPatientContextRequest):
    """Fetch merged local + EHR patient context for clinical intelligence."""
    from app.ehr_client import fetch_patient_context

    ctx = fetch_patient_context(phone_e164=req.phone_e164)
    return {
        "external_patient_id": ctx.external_patient_id,
        "external_mrn": ctx.external_mrn,
        "active_programs": ctx.active_programs,
        "conditions": ctx.conditions,
        "medications": ctx.medications,
        "allergies": ctx.allergies,
        "risk_factors": ctx.risk_factors,
    }


@app.get("/healthz")
def healthz():
    # Railway auto-injects these at build/deploy time; surfacing them lets anyone
    # confirm exactly which commit is actually live without guessing from bot replies.
    return {
        "ok": True,
        "git_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA") or None,
        "git_branch": os.getenv("RAILWAY_GIT_BRANCH") or None,
        "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or None,
    }


@app.get("/healthz/clinical")
def healthz_clinical():
    from app.clinical_intelligence import clinical_intelligence_enabled, load_action_paths
    from app.ehr_client import ehr_enabled, test_ehr_connectivity
    from app.google_vision import is_vision_configured

    return {
        "clinical_intelligence": clinical_intelligence_enabled(),
        "vision_ocr": is_vision_configured(),
        "ehr_api": ehr_enabled(),
        "ehr": test_ehr_connectivity(),
        "action_paths_loaded": bool(load_action_paths()),
    }


@app.get("/healthz/translation")
def healthz_translation():
    """Smoke-test Pidgin Marian (en→pcm). Compare `pidgin` to `english` — they should differ."""
    from app.neural_marian import MARIAN_MODEL_IDS, translate_neural
    from app.translation import translate_outbound_for_user

    sample = "Drink plenty of water and rest if you feel unwell."
    neural = translate_neural(text=sample, source_lang="en", target_lang="pcm")
    outbound = translate_outbound_for_user(sample, "pcm")
    pidgin_text = neural[0] if neural else None
    return {
        "pcm_models": list(MARIAN_MODEL_IDS.values()),
        "english_sample": sample,
        "marian_en_to_pcm": {
            "ok": neural is not None and bool((pidgin_text or "").strip()),
            "pidgin": pidgin_text,
            "model": neural[2] if neural else None,
            "same_as_english": (pidgin_text or "").strip() == sample.strip(),
        },
        "outbound_for_user_pcm": {
            "ok": outbound.strip() != sample.strip(),
            "text": outbound,
        },
    }


@app.get("/healthz/voice")
def healthz_voice():
    """WhatsApp voice STT/TTS configuration (no live API calls)."""
    from app.whatsapp_voice import voice_status

    return voice_status()


@app.get("/healthz/messaging")
def healthz_messaging(include_live_checks: bool = True):
    """
    Messaging diagnostics:
    - reports configured/not configured for Twilio / Africa's Talking / Meta WhatsApp
    - shows which provider path is currently active per channel
    - includes quick misconfiguration hints
    """
    integ = get_effective_integration()
    twilio_ready = bool(
        (integ.twilio_account_sid or "").strip()
        and (integ.twilio_auth_token or "").strip()
        and (integ.twilio_messaging_service_sid or "").strip()
    )
    at_ready = at_sms_ready()
    meta_ready = meta_wa.is_configured()

    active_sms_provider = "africastalking" if at_ready else ("twilio" if twilio_ready else "noop")
    active_whatsapp_provider = "meta" if meta_ready else ("twilio" if twilio_ready else "noop")
    hints: list[str] = []

    if not twilio_ready and not at_ready:
        hints.append("SMS delivery is disabled: configure Twilio or Africa's Talking credentials.")
    if twilio_ready and (integ.hc_base_url or "").strip().lower().startswith("http://"):
        hints.append("HC_BASE_URL is http:// ; Twilio status callback will not be attached (prefer https://).")
    op_url = (integ.operator_webhook_url or "").strip()
    if op_url and not (op_url.startswith("http://") or op_url.startswith("https://")):
        hints.append("HC_OPERATOR_WEBHOOK_URL must be a full URL (http/https); current value is invalid.")
    if not (integ.hc_sms_join_number or "").strip():
        hints.append("HC_SMS_JOIN_NUMBER is empty; join page cannot show an SMS number.")

    live_checks: dict[str, object] = {}
    if include_live_checks and twilio_ready:
        try:
            sid = (integ.twilio_account_sid or "").strip()
            tok = (integ.twilio_auth_token or "").strip()
            r = requests.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                auth=(sid, tok),
                timeout=10,
            )
            live_checks["twilio_auth"] = {
                "ok": 200 <= r.status_code < 300,
                "status_code": r.status_code,
                "error_preview": None if 200 <= r.status_code < 300 else (r.text or "")[:220],
            }
            if not (200 <= r.status_code < 300):
                hints.append(
                    "Twilio credentials are set but failed live auth check. Verify Account SID/Auth Token and whether Integration settings override env vars."
                )
        except Exception as exc:
            live_checks["twilio_auth"] = {"ok": False, "error": str(exc)[:220]}
            hints.append("Twilio live auth check failed due to network/runtime error.")
    elif include_live_checks:
        live_checks["twilio_auth"] = {"ok": False, "skipped": "twilio_not_ready"}

    return {
        "ok": True,
        "active_paths": {
            "sms": active_sms_provider,
            "whatsapp": active_whatsapp_provider,
        },
        "configured": {
            "twilio": {
                "ready": twilio_ready,
                "account_sid_set": bool((integ.twilio_account_sid or "").strip()),
                "auth_token_set": bool((integ.twilio_auth_token or "").strip()),
                "messaging_service_sid_set": bool((integ.twilio_messaging_service_sid or "").strip()),
            },
            "africastalking": {
                "ready": at_ready,
                "username_set": bool((integ.at_username or "").strip()),
                "api_key_set": bool((integ.at_api_key or "").strip()),
                "shortcode_set": bool((integ.at_shortcode or "").strip()),
            },
            "meta_whatsapp": {
                "ready": meta_ready,
            },
        },
        "integration": {
            "hc_base_url": integ.hc_base_url,
            "hc_sms_join_number": integ.hc_sms_join_number,
            "helpdesk_phone_e164": integ.helpdesk_phone_e164,
            "operator_webhook_url": integ.operator_webhook_url,
        },
        "live_checks": live_checks,
        "hints": hints,
    }

