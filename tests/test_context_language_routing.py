import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.inbound_chat import handle_inbound_text_message


class ContextAndLanguageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            lang_command_keyword="LANG",
            appoint_keyword="APPOINT",
            escalate_keyword="ESCALATE",
            rpm_keyword="RPM",
            telehealth_keyword="TELEHEALTH",
            rate_keyword="RATE",
            triage_keyword="TRIAGE",
            reschedule_keyword="RESCHEDULE",
            auto_reply_enabled=True,
            unknown_fallback_text="fallback",
        )

    def _route(self, body: str, *, user_lang: str = "en", active_programs=None):
        if active_programs is None:
            active_programs = []
        with patch("app.inbound_chat.record_inbound"), patch(
            "app.inbound_chat.get_user_language", return_value=user_lang
        ), patch("app.inbound_chat.translate_outbound_for_user", side_effect=lambda msg, _lang: msg), patch(
            "app.inbound_chat.text_for_english_pipeline", side_effect=lambda msg, _lang: msg
        ), patch(
            "app.inbound_chat.get_effective_integration",
            return_value=SimpleNamespace(helpdesk_phone_e164=""),
        ), patch("app.inbound_chat.get_app_settings", return_value=self.cfg), patch(
            "app.inbound_chat.get_keywords_for_channel",
            side_effect=lambda _channel, kind: {
                "join": ["JOIN"],
                "help": ["INFO", "MENU", "COMMANDS"],
                "symptom_check": ["CHECK", "SYMPTOMS"],
                "stop": ["STOP"],
                "start": ["START"],
            }[kind],
        ), patch("app.inbound_chat.active_program_codes_for_phone_e164", return_value=active_programs), patch(
            "app.inbound_chat.handle_appointment_message", return_value=None
        ), patch("app.inbound_chat.handle_telehealth_message", return_value=None), patch(
            "app.inbound_chat.is_in_appointment_flow", return_value=False
        ), patch("app.inbound_chat.is_in_telehealth_flow", return_value=False), patch(
            "app.inbound_chat.get_active_triage_session", return_value=None
        ), patch("app.inbound_chat.clinical_intelligence_enabled", return_value=False):
            return handle_inbound_text_message(
                channel="whatsapp",
                from_phone_raw="08010000000",
                to_phone_raw=None,
                body=body,
                raw={},
            )

    def test_pregnancy_food_question_returns_direct_guidance(self):
        with patch("app.inbound_chat.handle_symptom_question") as symptom_checker:
            reply = self._route("I'm pregnant, what foods should I eat?", active_programs=["maternal_child"])

        symptom_checker.assert_not_called()
        self.assertIn("During pregnancy, aim for regular balanced meals", reply)
        self.assertNotIn("Clinical triage", reply)

    def test_service_scope_question_does_not_force_triage(self):
        with patch("app.inbound_chat.handle_symptom_question") as symptom_checker:
            reply = self._route("Is this only for pregnant women?")

        symptom_checker.assert_not_called()
        self.assertIn("not only for pregnant women", reply)

    def test_lang_command_shows_language_menu(self):
        reply = self._route("LANG")

        self.assertIn("Choose your language for replies", reply)
        self.assertIn("reply English", reply)
        self.assertIn("reply Yoruba", reply)

    def test_plain_language_name_updates_language(self):
        with patch("app.inbound_chat.set_language") as set_language:
            reply = self._route("Yoruba")

        set_language.assert_called_once()
        self.assertIn("Language updated to YO", reply)


if __name__ == "__main__":
    unittest.main()