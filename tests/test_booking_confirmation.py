import unittest
from unittest.mock import patch

from app.appointments import (
    cancel_appointment_flow,
    handle_appointment_message,
    _get_flow as _get_appt_flow,
)
from app.database import session_scope
from app.inbound_chat import _wa_bold, _wa_italic
from app.karevault_client import build_contextual_question
from app.media_intake import _extract_lab_summary
from app.telehealth import (
    CONFIRM_TOKENS,
    _call_instructions,
    cancel_telehealth_flow,
    format_provider_name,
    handle_telehealth_message,
    _get_flow as _get_th_flow,
)
from app.default_help_menu import DEFAULT_HELP_MENU_TEXT

PHONE = "+2348099990001"


def _set_appt_step(step: str, *, profession: str = "general_practice", provider_id=None) -> None:
    with session_scope() as s:
        flow = _get_appt_flow(s, PHONE, "whatsapp")
        flow.step = step
        flow.visit_type = "telehealth"
        flow.profession_code = profession
        flow.selected_provider_id = provider_id


def _set_th_step(step: str, *, profession: str = "general_practice", provider_id=None) -> None:
    with session_scope() as s:
        flow = _get_th_flow(s, PHONE)
        flow.step = step
        flow.profession_code = profession
        flow.selected_provider_id = provider_id


class AppointmentConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        cancel_appointment_flow(PHONE)
        cancel_telehealth_flow(PHONE)

    def tearDown(self) -> None:
        cancel_appointment_flow(PHONE)
        cancel_telehealth_flow(PHONE)

    def test_whatsapp_ready_message_prioritizes_meeting_link(self) -> None:
        reply = _call_instructions(
            "ths_test",
            channel="whatsapp",
            billing_status="paid",
            fee_ngn=15000,
            provider_name="Chidinma Obi",
            provider_contact_phone="+2348000000000",
            call_url="https://telehealthportal.karevault.com/call/patient?id=12345",
            appointment_when="2026-09-10 13:30",
        )
        self.assertIn("Join your video consultation here at your appointment time", reply)
        self.assertIn("https://telehealthportal.karevault.com/call/patient?id=12345", reply)
        self.assertNotIn("We will send your clinician's WhatsApp number", reply)
        self.assertNotIn("Open chat:", reply)

    def test_whatsapp_ready_message_never_promises_clinician_contact(self) -> None:
        reply = _call_instructions(
            "ths_test",
            channel="whatsapp",
            billing_status="paid",
            fee_ngn=15000,
            provider_name="Chidinma Obi",
            provider_contact_phone="+2348000000000",
            appointment_when="2026-09-10 13:30",
        )
        self.assertIn("Your secure video meeting link is being prepared.", reply)
        self.assertIn("contact helpdesk with ref TH-TEST", reply)
        self.assertNotIn("WhatsApp number", reply)
        self.assertNotIn("Open chat:", reply)

    def test_confirm_tokens_cover_qa_variants(self) -> None:
        for token in ("YES", "Y", "CONFIRM", "OK", "BOOK", "PROCEED"):
            self.assertIn(token, CONFIRM_TOKENS)

    def test_yes_variants_trigger_video_booking(self) -> None:
        for body in ("YES", "Yes", "yes", "YES.", "yes please", "Confirm", "BOOK", "proceed"):
            _set_appt_step("confirm", provider_id="prov_test")
            with patch(
                "app.appointments._book_video_appointment", return_value="BOOKED"
            ) as book:
                reply = handle_appointment_message(
                    phone_e164=PHONE, channel="whatsapp", body=body
                )
            book.assert_called_once()
            self.assertEqual(reply, "BOOKED")

    def test_yes_variants_trigger_clinic_booking(self) -> None:
        for body in ("YES", "Yes.", "confirm", "ok"):
            _set_appt_step("clinic_confirm")
            with patch(
                "app.appointments._book_clinic_appointment", return_value="BOOKED"
            ) as book:
                reply = handle_appointment_message(
                    phone_e164=PHONE, channel="whatsapp", body=body
                )
            book.assert_called_once()
            self.assertEqual(reply, "BOOKED")

    def test_unrecognized_confirm_reply_stays_in_flow(self) -> None:
        _set_appt_step("confirm", provider_id="prov_test")
        reply = handle_appointment_message(
            phone_e164=PHONE, channel="whatsapp", body="maybe tomorrow"
        )
        self.assertIsNotNone(reply)
        self.assertIn("reply YES", reply)

    def test_free_text_in_provider_step_reprompts_instead_of_falling_through(self) -> None:
        _set_appt_step("provider")
        reply = handle_appointment_message(
            phone_e164=PHONE, channel="whatsapp", body="what is the fee?"
        )
        self.assertIsNotNone(reply)
        self.assertIn("number", reply)

    def test_router_commands_still_escape_the_flow(self) -> None:
        _set_appt_step("confirm", provider_id="prov_test")
        reply = handle_appointment_message(
            phone_e164=PHONE, channel="whatsapp", body="TELEHEALTH"
        )
        self.assertIsNone(reply)


class TelehealthConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        cancel_telehealth_flow(PHONE)

    def tearDown(self) -> None:
        cancel_telehealth_flow(PHONE)

    def test_punctuated_yes_is_recognized(self) -> None:
        # No provider selected: a recognized YES reaches the session-expired branch
        # instead of falling through to the generic handler.
        _set_th_step("confirm", provider_id=None)
        reply = handle_telehealth_message(phone_e164=PHONE, channel="whatsapp", body="Yes.")
        self.assertIsNotNone(reply)
        self.assertIn("Session expired", reply)

    def test_unrecognized_confirm_reply_stays_in_flow(self) -> None:
        _set_th_step("confirm", provider_id=None)
        reply = handle_telehealth_message(
            phone_e164=PHONE, channel="whatsapp", body="how much is it?"
        )
        self.assertIsNotNone(reply)
        self.assertIn("reply YES", reply)


class ProviderNameFormattingTests(unittest.TestCase):
    def test_title_is_not_duplicated(self) -> None:
        self.assertEqual(format_provider_name("Dr.", "Dr. Fatima Yusuf"), "Dr. Fatima Yusuf")
        self.assertEqual(format_provider_name("Dr", "Dr. Fatima Yusuf"), "Dr. Fatima Yusuf")

    def test_title_is_added_when_missing(self) -> None:
        self.assertEqual(format_provider_name("Dr.", "Fatima Yusuf"), "Dr. Fatima Yusuf")

    def test_no_title(self) -> None:
        self.assertEqual(format_provider_name("", "Fatima Yusuf"), "Fatima Yusuf")
        self.assertEqual(format_provider_name(None, "Fatima Yusuf"), "Fatima Yusuf")


class WhatsAppFormattingTests(unittest.TestCase):
    def test_bold_wraps_each_non_empty_line(self) -> None:
        text = "Dr. KareVault: Hello\n\nHow do you feel?"
        self.assertEqual(
            _wa_bold(text), "*Dr. KareVault: Hello*\n\n*How do you feel?*"
        )

    def test_bold_is_idempotent(self) -> None:
        self.assertEqual(_wa_bold("*already bold*"), "*already bold*")

    def test_italic_wraps_each_non_empty_line(self) -> None:
        self.assertEqual(_wa_italic("Send INFO for commands."), "_Send INFO for commands._")


class KareVaultPromptTests(unittest.TestCase):
    def test_prompt_anchors_to_symptom(self) -> None:
        msg = build_contextual_question("bad headache 2 days", ["chronic"])
        self.assertIn("Anchor your reply", msg)
        self.assertIn("bad headache 2 days", msg)

    def test_prompt_requests_concise_reply(self) -> None:
        msg = build_contextual_question("fever", ["maternal_child"])
        self.assertIn("2-4 short sentences", msg)

    def test_prompt_delimits_patient_message(self) -> None:
        msg = build_contextual_question("swollen feet", [])
        self.assertIn("Patient message:\n", msg)

    def test_new_consultation_reset(self) -> None:
        msg = build_contextual_question("dizzy", [], new_consultation=True)
        self.assertIn("NEW consultation", msg)


class LabSummaryExtractionTests(unittest.TestCase):
    def test_extracts_glucose_value(self) -> None:
        text = "Blood Glucose: 145 mg/dl\nHbA1c: 7.2%"
        summary = _extract_lab_summary(text)
        self.assertIn("Blood Glucose: 145", summary)
        self.assertIn("Hba1C: 7.2", summary)

    def test_extracts_creatinine(self) -> None:
        text = "Creatinine 1.8 mg/dl"
        summary = _extract_lab_summary(text)
        self.assertIn("Creatinine: 1.8", summary)

    def test_empty_text_returns_empty(self) -> None:
        self.assertEqual(_extract_lab_summary(None), "")
        self.assertEqual(_extract_lab_summary(""), "")

    def test_no_lab_values_returns_empty(self) -> None:
        self.assertEqual(_extract_lab_summary("Patient name: John Doe"), "")


class HelpMenuTrimTests(unittest.TestCase):
    def test_help_menu_is_concise(self) -> None:
        # Trimmed menu should be under 1200 chars (was ~1800+)
        self.assertLess(len(DEFAULT_HELP_MENU_TEXT), 1200)

    def test_help_menu_contains_key_commands(self) -> None:
        for cmd in ("INFO", "JOIN", "APPOINT", "TELEHEALTH", "Helpdesk", "RESET"):
            self.assertIn(cmd, DEFAULT_HELP_MENU_TEXT)


if __name__ == "__main__":
    unittest.main()
