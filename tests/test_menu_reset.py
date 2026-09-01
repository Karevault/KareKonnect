import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.inbound_chat import handle_inbound_text_message


class MenuAndResetRoutingTests(unittest.TestCase):
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

    def _route(self, body: str, active_programs: list[str] | None = None) -> str:
        with patch("app.inbound_chat.record_inbound"), patch(
            "app.inbound_chat.get_user_language", return_value="en"
        ), patch("app.inbound_chat.translate_outbound_for_user", side_effect=lambda msg, _lang: msg), patch(
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
        ), patch("app.inbound_chat.active_program_codes_for_phone_e164", return_value=active_programs or []), patch(
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

    def test_reset_command_returns_recovery_menu(self):
        with patch("app.inbound_chat.cancel_appointment_flow") as cancel_appt, patch(
            "app.inbound_chat.cancel_telehealth_flow"
        ) as cancel_telehealth, patch(
            "app.inbound_chat.cancel_active_triage_session", return_value=True
        ) as cancel_triage:
            reply = self._route("RESET", active_programs=["maternal_child"])

        cancel_appt.assert_called_once()
        cancel_telehealth.assert_called_once()
        cancel_triage.assert_called_once()
        self.assertIn("cleared your active conversation flow", reply)
        self.assertIn("send RESET", reply)
        self.assertIn("APPOINT or TELEHEALTH", reply)

    def test_plain_greeting_returns_starter_menu_for_new_user(self):
        reply = self._route("Hello")

        self.assertIn("Welcome to Kareconnect", reply)
        self.assertIn("send JOIN", reply)
        self.assertIn("send INFO, MENU, COMMANDS", reply)
        self.assertIn("send LANG", reply)


if __name__ == "__main__":
    unittest.main()