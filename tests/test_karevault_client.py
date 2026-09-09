import unittest
from unittest.mock import MagicMock, patch

from app.karevault_client import fetch_symptom_checker_reply


class KareVaultSymptomCheckerContractTests(unittest.TestCase):
    def test_uses_returned_remote_session_id_for_follow_up(self):
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
            first_result = fetch_symptom_checker_reply(
                question="I have a headache",
                programs=[],
                session_id="11111111-1111-4111-8111-111111111111",
            )
            second_result = fetch_symptom_checker_reply(
                question="It started last night and is a 5 out of 10.",
                programs=[],
                session_id="11111111-1111-4111-8111-111111111111",
            )

        self.assertEqual(first_result, "Please tell me when the headache started.")
        self.assertEqual(second_result, "Please tell me when the headache started.")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], {
            "message": "Context: general health education patient (programme not specified). "
            "You support an SMS/WhatsApp health education service. "
            "Anchor your reply to the symptom or concern the patient describes below. "
            "Reply in 2-4 short sentences — practical and direct. "
            "Clearly say when to seek urgent in-person or emergency care. "
            "Do not give specific drug doses. Patient message:\nI have a headache",
        })
        self.assertEqual(post.call_args_list[1].kwargs["json"]["session_id"], "remote-session")
        post.assert_any_call(
            "https://symptoms-checker.karevault.com/api/ai/chat/",
            json={
                "message": "Context: general health education patient (programme not specified). "
                "You support an SMS/WhatsApp health education service. "
                "Anchor your reply to the symptom or concern the patient describes below. "
                "Reply in 2-4 short sentences — practical and direct. "
                "Clearly say when to seek urgent in-person or emergency care. "
                "Do not give specific drug doses. Patient message:\nIt started last night and is a 5 out of 10.",
                "session_id": "remote-session",
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
