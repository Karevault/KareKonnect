from __future__ import annotations

from datetime import date
from typing import Literal, Tuple

from app.africastalking import normalize_at_phone_number
from app.services import enroll_programs, enqueue_due_messages, get_app_settings, send_queued_messages
from app.util import normalize_phone_e164


def run_ussd_step(phone_e164: str, text: str) -> Tuple[Literal["CON", "END"], str]:
    """
    USSD session step. `text` is Africa's Talking style path (segments joined by *).
    See: https://developers.africastalking.com/docs/ussd/overview
    """
    cfg = get_app_settings()
    phone = normalize_phone_e164(normalize_at_phone_number(phone_e164))
    steps = [s for s in (text or "").split("*") if s != ""]

    if len(steps) == 0:
        return (
            "CON",
            f"{cfg.ussd_welcome_message}\nDial: {cfg.ussd_code}",
        )

    if len(steps) == 1:
        choice = steps[0]
        if choice not in ("1", "2", "3", "4"):
            return "CON", cfg.ussd_invalid_choice_message
        if choice == "1":
            return "CON", "Enter EDD (YYYY-MM-DD):"
        _uid, newly_enrolled = enroll_programs(
            phone_e164=phone,
            channel="ussd",
            language="en",
            programs=[{"2": "chronic", "3": "youth", "4": "kidney"}[choice]],  # type: ignore[list-item]
            edd_iso=None,
        )
        if not newly_enrolled:
            return "END", "You are already enrolled in Kareconnect."
        enqueue_due_messages()
        send_queued_messages()
        return "END", "You have joined Kareconnect. You will receive messages soon."

    if len(steps) == 2 and steps[0] == "1":
        edd = steps[1]
        try:
            date.fromisoformat(edd)
        except ValueError:
            return "CON", "EDD format should be YYYY-MM-DD. Try again:"
        _uid, newly_enrolled = enroll_programs(
            phone_e164=phone, channel="ussd", language="en", programs=["maternal_child"], edd_iso=edd
        )
        if not newly_enrolled:
            return "END", "You are already enrolled in Kareconnect."
        enqueue_due_messages()
        send_queued_messages()
        return "END", "You have joined Maternal/Child Kareconnect. You will receive messages soon."

    return "END", "Session ended."
