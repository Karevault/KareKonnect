import unittest

from app.telehealth import availability_label_from_statuses, new_booking_session_status


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


if __name__ == "__main__":
    unittest.main()