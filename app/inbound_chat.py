from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional

from app.africastalking import normalize_at_phone_number
from app.integration_config import get_effective_integration
from app.services import (
    deliver_helpdesk_staff_reply,
    enroll_programs,
    enqueue_due_messages,
    get_app_settings,
    get_help_menu_text,
    get_keywords_for_channel,
    get_user_language,
    handle_helpdesk_question,
    record_helpdesk_hint_shown,
    should_show_helpdesk_hint,
    handle_symptom_question,
    record_inbound,
    request_escalation,
    request_rpm,
    send_queued_messages,
    set_enrollment_status,
    set_language,
    set_recurring_programme_opt_in,
)
from app.whatsapp_voice import set_whatsapp_voice_replies_opt_in
from app.default_help_menu import DEFAULT_SYMPTOM_CHECK_REPLY
from app.pcm_phrases import PCM_PHRASES
from app.translation import text_for_english_pipeline, translate_outbound_for_user
from app.appointments import (
    cancel_appointment_flow,
    handle_appointment_message,
    is_in_appointment_flow,
    start_appointment_flow,
)
from app.clinical_intelligence import clinical_intelligence_enabled, format_assessment_for_patient, run_clinical_assessment
from app.services import active_program_codes_for_phone_e164
from app.triage import (
    assessment_program_context,
    cancel_active_triage_session,
    current_intake_prompt,
    get_active_triage_session,
    intake_briefing_due,
    intake_complete,
    process_triage_onboarding,
    start_triage_session,
    triage_status_text,
    triage_welcome_addon,
)
from app.telehealth import (
    cancel_telehealth_flow,
    handle_telehealth_message,
    is_in_telehealth_flow,
    parse_rate_command,
    start_telehealth_flow,
    submit_rating,
)
from app.util import normalize_inbound_body, normalize_phone_e164


def _first_command_token(upper_line: str) -> str:
    """Match JOIN / INFO / MENU, etc., even when WhatsApp sends JOIN. or hidden characters around the word."""
    parts = upper_line.strip().split()
    if not parts:
        return ""
    return "".join(c for c in parts[0] if c.isalnum())


_HELPDESK_PREFIX_RE = re.compile(r"^\s*helpdesk\b", re.IGNORECASE)
_GREETING_ONLY_RE = re.compile(
    r"^(hi|hello|hey|good morning|good afternoon|good evening|morning|afternoon|evening)$",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(r"^(what|which|how|can|could|should|is|are|do|does|did|where|when|who)\b", re.I)
_DIET_QUERY_RE = re.compile(
    r"\b(food|foods|eat|eating|diet|nutrition|meal|meals|drink|drinks|feeding)\b",
    re.I,
)
_PREGNANCY_CONTEXT_RE = re.compile(r"\b(pregnan\w*|antenatal|baby|newborn|postpartum|breastfeed\w*)\b", re.I)
_GLUCOSE_CONTEXT_RE = re.compile(r"\b(diabet|sugar|glucose|bp sugar|blood sugar)\b", re.I)
_SYMPTOM_WORD_RE = re.compile(
    r"\b(pain|ache|fever|bleeding|cough|vomit|vomiting|diarrh|stool|headache|rash|wound|swollen|swelling|dizz|nausea|weak|faint|breath|itch|burning|discharge|cramp|injury|cut|ulcer)\b",
    re.I,
)
_SERVICE_SCOPE_RE = re.compile(r"\b(only for pregnant women|only pregnant|pregnant women only|for men too|for children too|only for women)\b", re.I)

_LANGUAGE_NAME_TO_CODE = {
    "english": "en",
    "en": "en",
    "pidgin": "pcm",
    "nigerian pidgin": "pcm",
    "pcm": "pcm",
    "hausa": "ha",
    "ha": "ha",
    "igbo": "ig",
    "ibo": "ig",
    "ig": "ig",
    "yoruba": "yo",
    "yo": "yo",
}


def _tail_menu_hint(help_keywords: list[str]) -> str:
    bits = ", ".join(help_keywords[:4]) if help_keywords else "INFO, MENU, COMMANDS"
    return f" Send {bits} for the full command list."


def _language_menu_text(lang_cmd: str) -> str:
    cmd = lang_cmd.upper().strip() or "LANG"
    return (
        "Choose your language for replies:\n"
        f"• {cmd} EN or reply English\n"
        f"• {cmd} PCM or reply Pidgin\n"
        f"• {cmd} HA or reply Hausa\n"
        f"• {cmd} IG or reply Igbo\n"
        f"• {cmd} YO or reply Yoruba"
    )


def _looks_like_language_prompt(body: str, *, lang_cmd: str) -> bool:
    normalized = " ".join((body or "").strip().lower().split())
    return normalized in {
        lang_cmd.lower().strip(),
        "language",
        "change language",
        "set language",
        "language menu",
        "choose language",
    }


def _parse_language_choice(body: str, *, lang_cmd: str) -> Optional[str]:
    normalized = " ".join((body or "").strip().lower().split())
    if not normalized:
        return None
    if normalized.startswith(f"{lang_cmd.lower().strip()} "):
        normalized = normalized.split(" ", 1)[1].strip()
    elif normalized.startswith("language "):
        normalized = normalized.split(" ", 1)[1].strip()
    elif normalized.startswith("use "):
        normalized = normalized.split(" ", 1)[1].strip()
    elif normalized.startswith("reply in "):
        normalized = normalized.split(" ", 2)[2].strip()
    elif normalized.startswith("i prefer "):
        normalized = normalized.split(" ", 2)[2].strip()
    elif normalized.startswith("i want "):
        normalized = normalized.split(" ", 2)[2].strip()
    elif normalized.startswith("speak "):
        normalized = normalized.split(" ", 1)[1].strip()
    normalized = normalized.replace(" language", "").strip()
    return _LANGUAGE_NAME_TO_CODE.get(normalized)


def _is_question_like(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.endswith("?") or bool(_QUESTION_START_RE.match(stripped))


def _informational_health_reply(body_en: str, *, help_keywords: list[str]) -> Optional[str]:
    text = " ".join((body_en or "").strip().split())
    if not text:
        return None
    lower = text.lower()
    menu_hint = _tail_menu_hint(help_keywords)
    if _SERVICE_SCOPE_RE.search(lower):
        return (
            "Kareconnect is not only for pregnant women. We also support maternal and newborn care, chronic disease follow-up, youth health, kidney care, appointments, and telehealth. "
            "If you want a health assessment, describe the main symptom in one short message."
            + menu_hint
        )
    if _DIET_QUERY_RE.search(lower) and _PREGNANCY_CONTEXT_RE.search(lower):
        return (
            "During pregnancy, aim for regular balanced meals: beans, eggs, fish, meat, vegetables, fruits, whole grains, and plenty of safe water. "
            "Limit alcohol, smoking, and sugary drinks, and ask your clinician before using herbs or supplements. "
            "If you have vomiting, swelling, bleeding, severe headache, or reduced baby movement, describe that symptom separately for urgent guidance."
            + menu_hint
        )
    if _DIET_QUERY_RE.search(lower) and _GLUCOSE_CONTEXT_RE.search(lower):
        return (
            "For blood sugar control, try smaller carbohydrate portions, add vegetables and protein to meals, avoid sugary drinks, and stay active if it is safe for you. "
            "If you want medication or symptom advice, send the main symptom or your recent reading in one short message."
            + menu_hint
        )
    if _DIET_QUERY_RE.search(lower):
        return (
            "For general healthy eating, aim for balanced meals with vegetables, fruit, beans, eggs, fish, or other protein, and reduce sugary drinks and highly processed snacks. "
            "If this question is about pregnancy, diabetes, or kidney disease, say that in your next message so we can tailor the guidance."
            + menu_hint
        )
    return None


def _should_treat_as_informational_query(body_en: str, *, faq_suggested: Optional[str]) -> bool:
    text = (body_en or "").strip()
    if not text:
        return False
    if _informational_health_reply(text, help_keywords=[]) is not None:
        return True
    if faq_suggested and _is_question_like(text) and not _SYMPTOM_WORD_RE.search(text):
        return True
    return False


def _is_reset_request(upper_line: str, first_token: str) -> bool:
    normalized = " ".join((upper_line or "").split())
    return first_token == "RESET" or normalized in {
        "RESET",
        "RESTART",
        "START OVER",
        "STARTOVER",
        "START AGAIN",
        "CLEAR HISTORY",
        "CLEAR CHAT",
    }


def _looks_like_plain_greeting(body: str) -> bool:
    return bool(_GREETING_ONLY_RE.fullmatch((body or "").strip()))


def _starter_menu_text(
    *,
    help_keywords: list[str],
    cfg,
    channel: Literal["sms", "whatsapp"],
    enrolled: bool,
) -> str:
    bits = ", ".join(help_keywords[:3]) if help_keywords else "INFO, MENU, COMMANDS"
    lang_cmd = cfg.lang_command_keyword.upper().strip() or "LANG"
    lines = ["Welcome to Kareconnect.", "", "Quick start:"]
    if enrolled:
        lines.append("1. Health question — send one short message in your own words.")
    else:
        lines.append("1. Join this service — send JOIN.")
    lines.extend(
        [
            f"2. Full menu — send {bits}.",
            f"3. Book care — send {cfg.appoint_keyword.upper().strip()} or {cfg.telehealth_keyword.upper().strip()}.",
            f"4. Choose language — send {lang_cmd}, or reply English, Pidgin, Hausa, Igbo, or Yoruba.",
            "5. Restart anytime — send RESET or START OVER.",
        ]
    )
    if channel == "whatsapp":
        lines.append("6. On WhatsApp, you can also send photos, PDF results, or a voice note.")
    return "\n".join(lines)


def _reset_conversation_state(phone_e164: str) -> bool:
    appt_active = is_in_appointment_flow(phone_e164)
    telehealth_active = is_in_telehealth_flow(phone_e164)
    triage_active = get_active_triage_session(phone_e164) is not None
    cancel_appointment_flow(phone_e164)
    cancel_telehealth_flow(phone_e164)
    triage_closed = cancel_active_triage_session(phone_e164)
    return appt_active or telehealth_active or triage_active or triage_closed


def _symptom_reply_cmd_tail(
    *,
    from_phone: str,
    channel: Literal["sms", "whatsapp"],
    help_keywords: list[str],
    cfg,
) -> str:
    bits = ", ".join(help_keywords[:3]) if help_keywords else "INFO, MENU, COMMANDS"
    cmds = (
        f"Commands: {bits} — {cfg.lang_command_keyword} EN, {cfg.appoint_keyword} …, "
        f"{cfg.telehealth_keyword}, {cfg.escalate_keyword} ROUTINE …, {cfg.rpm_keyword} …"
    )
    if should_show_helpdesk_hint(phone_e164=from_phone):
        record_helpdesk_hint_shown(phone_e164=from_phone, channel=channel)
        return f"Need our team? Start your next message with Helpdesk. {cmds}"
    return cmds


def _is_helpdesk_message(body: str) -> bool:
    return bool(_HELPDESK_PREFIX_RE.match(body or ""))


def _strip_helpdesk_prefix(body: str) -> str:
    return re.sub(r"^\s*helpdesk\s*", "", body or "", count=1, flags=re.IGNORECASE).strip()


def _join_welcome_text(help_keywords: list[str], channel: Literal["sms", "whatsapp"]) -> str:
    """English body; outbound translation applies when the user has LANG set to a non-English code."""
    bits = ", ".join(help_keywords[:4]) if help_keywords else "INFO, MENU, COMMANDS"
    core = (
        "Welcome to Kareconnect. You are now enrolled.\n\n"
        "Programmes: maternal & child, chronic disease, youth health, kidney care.\n\n"
        "1. Health questions — reply in your own words (not a command). "
        'Example: "bad headache two days" or "swollen feet". We use our FAQ and symptom checker.\n'
        "2. Talk to our team — start your message with Helpdesk. "
        'Example: Helpdesk I need to speak to a nurse.\n'
        f"3. All commands — send {bits} (LANG, STOP, APPOINT, …)."
    )
    if channel == "whatsapp":
        core += (
            "\n\nOptional: send REMINDERS YES for at most one WhatsApp programme education tip per day "
            "(send REMINDERS NO anytime to turn those tips off). "
            "Send a voice note with your question, or VOICE YES for short voice replies."
        )
    return core


def _looks_like_staff_ticket_reply(body: str) -> bool:
    """Staff helpdesk replies must look like TKT <id> … so the same number can still text JOIN/INFO as a patient."""
    return bool(re.match(r"^\s*TKT\s+\S+", (body or "").strip(), re.IGNORECASE))


def _parse_programs_csv(v: Optional[str]) -> list[str]:
    if not v:
        return []
    parts = [p.strip().lower() for p in v.replace(";", ",").split(",")]
    return [p for p in parts if p]


def handle_inbound_text_message(
    *,
    channel: Literal["sms", "whatsapp"],
    from_phone_raw: str,
    to_phone_raw: Optional[str],
    body: str,
    raw: dict[str, Any],
) -> str:
    """
    Shared SMS / WhatsApp (Twilio-style body) routing.
    Returns plain text to show to the user (Twilio will wrap in TwiML; Africa's Talking sends via API).
    """
    body = normalize_inbound_body(body)
    from_phone = normalize_phone_e164(normalize_at_phone_number(from_phone_raw))
    to_phone = (
        normalize_phone_e164(normalize_at_phone_number(to_phone_raw)) if to_phone_raw else None
    )

    record_inbound(
        channel=channel,
        from_phone=from_phone,
        to_phone=to_phone,
        body=body,
        raw={k: str(v) for k, v in raw.items()},
    )

    user_lang = get_user_language(from_phone)

    def out(msg: str) -> str:
        return translate_outbound_for_user(msg, user_lang)

    hp_raw = (get_effective_integration().helpdesk_phone_e164 or "").strip()
    if hp_raw:
        staff_phone = normalize_phone_e164(normalize_at_phone_number(hp_raw))
        if from_phone == staff_phone and _looks_like_staff_ticket_reply(body):
            return deliver_helpdesk_staff_reply(body=body, staff_inbound_channel=channel)

    cfg = get_app_settings()
    upper = body.upper().strip()
    first = _first_command_token(upper)
    join_keywords = get_keywords_for_channel(channel, "join")
    help_keywords = get_keywords_for_channel(channel, "help")
    symptom_check_keywords = get_keywords_for_channel(channel, "symptom_check")
    stop_keywords = get_keywords_for_channel(channel, "stop")
    start_keywords = get_keywords_for_channel(channel, "start")
    lang_cmd = cfg.lang_command_keyword.upper().strip()
    appoint_cmd = cfg.appoint_keyword.upper().strip()
    escalate_cmd = cfg.escalate_keyword.upper().strip()
    rpm_cmd = cfg.rpm_keyword.upper().strip()
    telehealth_cmd = cfg.telehealth_keyword.upper().strip()
    rate_cmd = cfg.rate_keyword.upper().strip()
    triage_cmd = cfg.triage_keyword.upper().strip()
    active_programs = active_program_codes_for_phone_e164(from_phone)

    # INFO / MENU / COMMANDS — always return the full help menu (escape hatch from any flow).
    if first in help_keywords or upper in help_keywords:
        return out(get_help_menu_text())

    if _looks_like_language_prompt(body, lang_cmd=cfg.lang_command_keyword):
        return out(_language_menu_text(cfg.lang_command_keyword))

    selected_lang = _parse_language_choice(body, lang_cmd=cfg.lang_command_keyword)
    if selected_lang in ("en", "pcm", "ha", "ig", "yo"):
        set_language(phone_e164=from_phone, language=selected_lang)  # type: ignore[arg-type]
        user_lang = selected_lang  # type: ignore[assignment]
        if selected_lang == "pcm":
            return PCM_PHRASES["language_updated"] + _tail_menu_hint(help_keywords)
        return translate_outbound_for_user(
            f"Language updated to {selected_lang.upper()}." + _tail_menu_hint(help_keywords),
            selected_lang,
        )

    if _is_reset_request(upper, first):
        had_state = _reset_conversation_state(from_phone)
        starter = _starter_menu_text(
            help_keywords=help_keywords,
            cfg=cfg,
            channel=channel,
            enrolled=bool(active_programs),
        )
        if had_state:
            return out(
                "Okay — I cleared your active conversation flow. Your enrolment and message history are still on file.\n\n"
                + starter
            )
        return out("There was no active conversation flow to clear.\n\n" + starter)

    appt_reply = handle_appointment_message(phone_e164=from_phone, channel=channel, body=body)
    if appt_reply is not None:
        return out(appt_reply)

    th_reply = handle_telehealth_message(phone_e164=from_phone, channel=channel, body=body)
    if th_reply is not None:
        return out(th_reply)

    if first == triage_cmd or upper == triage_cmd:
        return out(triage_status_text(from_phone))

    if first == telehealth_cmd or upper == telehealth_cmd:
        return out(start_telehealth_flow(phone_e164=from_phone))

    if first == rate_cmd or upper.startswith(f"{rate_cmd} "):
        score, comment = parse_rate_command(body, keyword=cfg.rate_keyword)
        if score is None:
            return out(
                f"Send {rate_cmd} 1-5 with an optional comment. Example: {rate_cmd} 4 Good advice"
            )
        return out(submit_rating(phone_e164=from_phone, score=score, comment=comment))

    if first == "VOICE" and channel == "whatsapp":
        parts_u = upper.split()
        sub = parts_u[1] if len(parts_u) > 1 else ""
        if sub in ("YES", "ON", "1", "TRUE"):
            set_whatsapp_voice_replies_opt_in(phone_e164=from_phone, enabled=True)
            return out(
                "Voice replies are ON for short WhatsApp answers (when our system supports it). "
                "You will still receive text for long messages. Send VOICE NO to turn off."
                + _tail_menu_hint(help_keywords)
            )
        if sub in ("NO", "OFF", "0", "FALSE"):
            set_whatsapp_voice_replies_opt_in(phone_e164=from_phone, enabled=False)
            return out(
                "Voice replies are OFF. Replies will be text only. Send VOICE YES to opt in again."
                + _tail_menu_hint(help_keywords)
            )
        return out(
            'On WhatsApp: send "VOICE YES" for short voice-note replies, or "VOICE NO" for text only.'
            + _tail_menu_hint(help_keywords)
        )

    if first == "REMINDERS":
        parts_u = upper.split()
        sub = parts_u[1] if len(parts_u) > 1 else ""
        if sub in ("YES", "ON", "1", "TRUE"):
            if channel != "whatsapp":
                return out(
                    "Recurring programme tips are only available on WhatsApp. "
                    "Use WhatsApp on this number or open our enrolment link and send REMINDERS YES from WhatsApp."
                )
            set_recurring_programme_opt_in(phone_e164=from_phone, enabled=True)
            return out(
                "You're opted in to WhatsApp programme education tips (at most one tip per day when you're enrolled). "
                "Send REMINDERS NO to turn those tips off. STOP still pauses all programme messages; START resumes them."
            )
        if sub in ("NO", "OFF", "0", "FALSE"):
            set_recurring_programme_opt_in(phone_e164=from_phone, enabled=False)
            return out(
                "WhatsApp programme tips are off. Send REMINDERS YES on WhatsApp to opt in again."
                + _tail_menu_hint(help_keywords)
            )
        return out(
            'On WhatsApp: send "REMINDERS YES" to opt in to daily programme education tips, or "REMINDERS NO" to opt out. '
            "Tips are WhatsApp-only."
            + _tail_menu_hint(help_keywords)
        )

    if first in join_keywords:
        parts = body.strip().split()
        programs = ["maternal_child"]
        edd = None
        if len(parts) >= 2:
            token = parts[1].strip()
            if len(token) == 10 and token[4] == "-" and token[7] == "-":
                edd = token
            else:
                programs = [p for p in _parse_programs_csv(token) if p in ("maternal_child", "chronic", "youth", "kidney")] or programs
        if len(parts) >= 3 and edd is None:
            token = parts[2].strip()
            if len(token) == 10 and token[4] == "-" and token[7] == "-":
                edd = token

        _user_id, newly_enrolled = enroll_programs(
            phone_e164=from_phone, channel=channel, language="en", programs=programs, edd_iso=edd
        )
        if not newly_enrolled:
            bits = ", ".join(help_keywords[:4]) if help_keywords else "INFO, MENU, COMMANDS"
            return out(
                "You're already enrolled in Kareconnect. "
                f"Send {bits} for commands. Send STOP to pause programme messages."
            )
        welcome = _join_welcome_text(help_keywords, channel)
        if newly_enrolled and channel == "whatsapp":
            triage_id = start_triage_session(phone_e164=from_phone, channel=channel)
            welcome += triage_welcome_addon() + "\n\n" + current_intake_prompt(triage_session_id=triage_id)
        return out(welcome)

    if upper.startswith(f"{lang_cmd} "):
        return out(
            "Unsupported language. Choose from English, Pidgin, Hausa, Igbo, or Yoruba. "
            f"Send {_language_menu_text(cfg.lang_command_keyword).replace(chr(10), '; ')}"
        )

    if first in stop_keywords:
        set_enrollment_status(phone_e164=from_phone, status="paused")
        if user_lang == "pcm":
            return PCM_PHRASES["messages_paused"]
        return out("Messages paused. Reply START to resume.")

    if first in start_keywords:
        set_enrollment_status(phone_e164=from_phone, status="active")
        enqueue_due_messages()
        send_queued_messages()
        if user_lang == "pcm":
            return PCM_PHRASES["messages_resumed"]
        return out("Messages resumed.")

    if upper.startswith(appoint_cmd):
        reason = body[len(cfg.appoint_keyword) :].strip() or None
        return out(start_appointment_flow(phone_e164=from_phone, channel=channel, reason=reason))

    if upper.startswith(escalate_cmd):
        rest = upper[len(cfg.escalate_keyword) :].strip()
        urgency = "routine"
        if "URGENT" in rest:
            urgency = "urgent"
        if "EMERGENCY" in rest:
            urgency = "emergency"
        esc_id = request_escalation(from_phone=from_phone, reason=body, urgency=urgency)
        return out(
            f"Telehealth escalation requested (ref {esc_id}). If this is an emergency, go to the nearest facility now."
            + _tail_menu_hint(help_keywords)
        )

    if upper.startswith(rpm_cmd):
        condition = body[len(cfg.rpm_keyword) :].strip() or "general"
        rpm_id = request_rpm(from_phone=from_phone, condition=condition, device_type=None)
        return out(
            f"RPM enrollment request received (ref {rpm_id}). A team member will contact you."
            + _tail_menu_hint(help_keywords)
        )

    if first in symptom_check_keywords:
        return out(DEFAULT_SYMPTOM_CHECK_REPLY)

    if _looks_like_plain_greeting(body) and not get_active_triage_session(from_phone):
        return out(
            _starter_menu_text(
                help_keywords=help_keywords,
                cfg=cfg,
                channel=channel,
                enrolled=bool(active_programs),
            )
        )

    if is_in_telehealth_flow(from_phone):
        return out(
            "You are in a telehealth booking. Reply with the number shown, YES to confirm, BACK, or CANCEL."
        )

    if is_in_appointment_flow(from_phone):
        return out(
            "You are in appointment booking. Reply with the number shown, YES to confirm, BACK, or CANCEL."
        )

    body_en = text_for_english_pipeline(body, user_lang)
    bits = ", ".join(help_keywords[:3]) if help_keywords else "INFO, MENU, COMMANDS"
    disc = "\n\n(Information only—not a diagnosis. Emergencies: go to the nearest facility.)"

    direct_info = _informational_health_reply(body_en, help_keywords=help_keywords)
    if direct_info:
        return out(direct_info)

    faq_only = None
    if _is_question_like(body_en):
        from app.util import best_faq_answer, load_faqs

        faq_only = best_faq_answer(body_en, load_faqs())
        if _should_treat_as_informational_query(body_en, faq_suggested=faq_only) and faq_only:
            return out(
                faq_only
                + "\n\nIf you want a clinical assessment, describe the main symptom in one short message."
                + _tail_menu_hint(help_keywords)
            )

    triage = None
    triage_id = None
    if clinical_intelligence_enabled():
        triage = get_active_triage_session(from_phone)
        if not triage:
            triage_id = start_triage_session(phone_e164=from_phone, channel=channel)
            triage = get_active_triage_session(from_phone)
        triage_id = triage.id if triage else None

    show_intake_briefing = intake_briefing_due(phone_e164=from_phone, triage_session_id=triage_id)
    cmd_tail = (
        _symptom_reply_cmd_tail(
            from_phone=from_phone,
            channel=channel,
            help_keywords=help_keywords,
            cfg=cfg,
        )
        if show_intake_briefing
        else ""
    )

    if _is_helpdesk_message(body):
        question = _strip_helpdesk_prefix(body)
        if not question:
            return out(
                "Start your message with Helpdesk and your question. "
                'Example: Helpdesk I need a callback.'
                + _tail_menu_hint(help_keywords)
            )
        ticket_id, suggested = handle_helpdesk_question(
            from_phone=from_phone,
            channel=channel,
            question=question,
            question_english=text_for_english_pipeline(question, user_lang),
        )
        cmd_tail = (
            f"We opened ticket {ticket_id}. Our team will follow up on this chat. "
            f"Send another message starting with Helpdesk to add more (ref {ticket_id}). "
            f"Commands: {bits}."
        )
        if suggested:
            return out(f"Suggested answer: {suggested}\n\n{cmd_tail}")
        if cfg.auto_reply_enabled:
            return out(f"{cfg.unknown_fallback_text}\n\n{cmd_tail}")
        return out(cmd_tail)

    if first in ("INFO", "MENU", "COMMANDS", "HELP") or upper in (
        "INFO",
        "MENU",
        "COMMANDS",
        "HELP",
    ):
        return out(get_help_menu_text())

    if clinical_intelligence_enabled() and triage_id and not intake_complete(triage_session_id=triage_id):
        intake_reply, chief_complaint = process_triage_onboarding(
            triage_session_id=triage_id,
            response_text=body_en,
        )
        if intake_reply:
            return out(intake_reply)
        if chief_complaint:
            body_en = chief_complaint
            body = chief_complaint

    program_context = assessment_program_context(
        programs=active_programs,
        triage_session_id=triage_id,
        chief_complaint=body_en,
    )

    suggested, kv = handle_symptom_question(
        from_phone=from_phone,
        question=body,
        question_english=body_en,
        programs=program_context,
    )
    kv_t = _truncate_reply(kv, 650)

    triage_block = ""
    if clinical_intelligence_enabled():
        try:
            from app.triage import set_chief_complaint

            if triage_id:
                set_chief_complaint(triage_session_id=triage_id, text=body_en)
            assessment = run_clinical_assessment(
                phone_e164=from_phone,
                patient_text=body_en,
                programs=program_context,
                triage_session_id=triage_id,
                source="text",
            )
            if show_intake_briefing:
                triage_block = format_assessment_for_patient(assessment)
                if triage_block:
                    triage_block = f"\n\n{triage_block}"
        except Exception:
            logging.getLogger(__name__).exception(
                "clinical assessment failed phone=%s", from_phone
            )

    def _append_cmd(msg: str) -> str:
        if cmd_tail:
            return f"{msg}\n\n{cmd_tail}"
        return msg

    if suggested and kv_t:
        return out(_append_cmd(f"Suggested answer: {suggested}{disc}\n\nMore info:\n{kv_t}{triage_block}"))
    if suggested:
        return out(_append_cmd(f"Suggested answer: {suggested}{disc}{triage_block}"))
    if kv_t:
        return out(_append_cmd(f"{kv_t}{disc}{triage_block}"))
    if triage_block:
        return out(_append_cmd(f"{triage_block.strip()}{disc}"))
    if cfg.auto_reply_enabled:
        return out(f"{cfg.unknown_fallback_text}" + _tail_menu_hint(help_keywords))
    return ""


def _truncate_reply(text: Optional[str], max_len: int) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"
