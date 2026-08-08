"""A delivered assessment ends the session; the next message returns to the main menu."""

from __future__ import annotations

import unittest

from app.triage import (
    complete_triage_session,
    consume_completed_consultation,
    get_active_triage_session,
    start_triage_session,
)

PHONE = "+2348039990001"


class ConsultationEndsAtMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        # Leave no session from an earlier run in a state that can leak into these tests.
        while consume_completed_consultation(PHONE):
            pass
        from app.triage import cancel_active_triage_session

        cancel_active_triage_session(PHONE)

    def test_completed_session_is_no_longer_active(self) -> None:
        tid = start_triage_session(phone_e164=PHONE, channel="whatsapp")
        self.assertIsNotNone(get_active_triage_session(PHONE))

        self.assertTrue(complete_triage_session(tid))
        self.assertIsNone(
            get_active_triage_session(PHONE),
            "a completed consultation must not stay active",
        )

    def test_menu_is_offered_exactly_once_after_completion(self) -> None:
        tid = start_triage_session(phone_e164=PHONE, channel="whatsapp")
        complete_triage_session(tid)

        # First message after the diagnosis -> welcome menu.
        self.assertTrue(consume_completed_consultation(PHONE))
        # Subsequent messages must not keep re-greeting.
        self.assertFalse(consume_completed_consultation(PHONE))

    def test_no_menu_while_consultation_is_still_open(self) -> None:
        start_triage_session(phone_e164=PHONE, channel="whatsapp")
        self.assertFalse(
            consume_completed_consultation(PHONE),
            "an in-progress consultation must not be interrupted by the menu",
        )

    def test_next_message_opens_a_brand_new_consultation(self) -> None:
        first = start_triage_session(phone_e164=PHONE, channel="whatsapp")
        complete_triage_session(first)
        consume_completed_consultation(PHONE)

        second = start_triage_session(phone_e164=PHONE, channel="whatsapp")
        self.assertNotEqual(
            first,
            second,
            "a new question after the menu must start a fresh consultation",
        )

    def test_completing_twice_is_a_no_op(self) -> None:
        tid = start_triage_session(phone_e164=PHONE, channel="whatsapp")
        self.assertTrue(complete_triage_session(tid))
        self.assertFalse(complete_triage_session(tid))
        self.assertFalse(complete_triage_session(None))


if __name__ == "__main__":
    unittest.main()
