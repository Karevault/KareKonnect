import unittest
from unittest.mock import patch

from app.database import session_scope
from app.models import TelehealthPatientFlow
from app.telehealth import (
    availability_label_from_statuses,
    handle_telehealth_message,
    new_booking_session_status,
    start_telehealth_flow,
)


class TelehealthBookingStatusTests(unittest.TestCase):
    def test_fee_bearing_booking_starts_pending_payment(self):
        self.assertEqual(new_booking_session_status(5000), "pending_payment")
        self.assertEqual(new_booking_session_status(1), "pending_payment")

    def test_free_booking_starts_confirmed(self):
        self.assertEqual(new_booking_session_status(0), "confirmed")

    def test_pending_payment_does_not_count_as_busy_booking(self):
        self.assertEqual(availability_label_from_statuses(["pending_payment"]), "awaiting_payment")
        self.assertEqual(availability_label_from_statuses(["confirmed"]), "booked")
        self.assertEqual(availability_label_from_statuses(["in_progress"]), "in_session")
        self.assertEqual(availability_label_from_statuses([]), "free")


class StartFlowNoOptionsTests(unittest.TestCase):
    """If the live doctor API returns nothing (e.g. blocked upstream), the patient must not
    be parked in a menu step with no valid options — that traps all later messages until
    they happen to send CANCEL."""

    PHONE = "+2348160000001"

    def tearDown(self):
        with session_scope() as s:
            s.query(TelehealthPatientFlow).filter(
                TelehealthPatientFlow.phone_e164 == self.PHONE
            ).delete()

    def test_empty_live_specialties_leaves_flow_idle(self):
        with patch(
            "app.karevault_telehealth_client.telehealth_api_enabled", return_value=True
        ), patch("app.telehealth.list_specialties_with_counts", return_value=[]):
            reply = start_telehealth_flow(phone_e164=self.PHONE)

        self.assertIn("not open yet", reply)
        with session_scope() as s:
            flow = s.execute(
                select_flow(self.PHONE)
            ).scalar_one()
            self.assertEqual(flow.step, "idle")

    def test_unrelated_message_not_swallowed_after_empty_start(self):
        with patch(
            "app.karevault_telehealth_client.telehealth_api_enabled", return_value=True
        ), patch("app.telehealth.list_specialties_with_counts", return_value=[]):
            start_telehealth_flow(phone_e164=self.PHONE)

        reply = handle_telehealth_message(
            phone_e164=self.PHONE, channel="whatsapp", body="Symptom checker"
        )
        # None means "not handled by the telehealth flow" — falls through to normal routing
        # instead of repeating "Reply with the number of a specialty...".
        self.assertIsNone(reply)


def select_flow(phone_e164: str):
    from sqlalchemy import select

    return select(TelehealthPatientFlow).where(TelehealthPatientFlow.phone_e164 == phone_e164)


if __name__ == "__main__":
    unittest.main()