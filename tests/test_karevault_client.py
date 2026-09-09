import unittest
from unittest.mock import MagicMock, patch

from app.karevault_client import fetch_symptom_checker_reply


class KareVaultSymptomCheckerContractTests(unittest.TestCase):
    def test_uses_documented_public_chat_endpoint_and_session_id(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "session_id": "remote-session",
            "message": "Please tell me when the headache started.",
            "stage": "gathering",
        }
        with patch.dict(
            "os.environ",
            {
                "HC_KAREVAULT_ENABLED": "true",
                "HC_KAREVAULT_BASE_URL": "https://symptoms-checker.karevault.com",
                "HC_KAREVAULT_PARTNER_EMAIL": "partner@example.com",
            },
            clear=False,
        ), patch("app.karevault_client.requests.post", return_value=response) as post:
            result = fetch_symptom_checker_reply(
                question="I have a headache",
                programs=[],
                session_id="11111111-1111-4111-8111-111111111111",
            )

        self.assertEqual(result, "Please tell me when the headache started.")
        post.assert_called_once_with(
            "https://symptoms-checker.karevault.com/api/ai/chat/",
            json={
                "message": "Context: general health education patient (programme not specified). "
                "You support an SMS/WhatsApp health education service. "
                "Anchor your reply to the symptom or concern the patient describes below. "
                "Reply in 2-4 short sentences — practical and direct. "
                "Clearly say when to seek urgent in-person or emergency care. "
                "Do not give specific drug doses. Patient message:\nI have a headache",
                "session_id": "11111111-1111-4111-8111-111111111111",
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

    def test_parses_documented_message_field(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"message": "Seek urgent care if symptoms worsen."}
        with patch.dict("os.environ", {"HC_KAREVAULT_ENABLED": "true"}), patch(
            "app.karevault_client.requests.post", return_value=response
        ):
            result = fetch_symptom_checker_reply(question="I feel unwell", programs=[])

        self.assertEqual(result, "Seek urgent care if symptoms worsen.")


if __name__ == "__main__":
    unittest.main()
