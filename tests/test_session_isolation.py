import unittest

from app.main import _reset_test_chat_state_for_identity


class TestChatSessionIsolationTests(unittest.TestCase):
    def test_identity_change_clears_prior_messages(self):
        state = {
            "channel": "whatsapp",
            "phone_e164": "+2348010000000",
            "to_phone_e164": "+14155238886",
            "messages": [{"role": "bot", "text": "old reply"}],
        }

        changed = _reset_test_chat_state_for_identity(
            state,
            channel="whatsapp",
            phone_e164="+2348020000000",
            to_phone_e164="+14155238886",
        )

        self.assertTrue(changed)
        self.assertEqual(state["phone_e164"], "+2348020000000")
        self.assertEqual(state["messages"], [])

    def test_same_identity_keeps_existing_messages(self):
        existing = [{"role": "bot", "text": "keep me"}]
        state = {
            "channel": "whatsapp",
            "phone_e164": "+2348010000000",
            "to_phone_e164": "+14155238886",
            "messages": existing,
        }

        changed = _reset_test_chat_state_for_identity(
            state,
            channel="whatsapp",
            phone_e164="+2348010000000",
            to_phone_e164="+14155238886",
        )

        self.assertFalse(changed)
        self.assertIs(state["messages"], existing)


if __name__ == "__main__":
    unittest.main()