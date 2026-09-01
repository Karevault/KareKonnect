import unittest
from unittest.mock import MagicMock, patch

from app.database import session_scope
from app.karevault_telehealth_client import (
    RemoteDoctor,
    _parse_fee,
    invalidate_doctors_cache,
    list_doctors,
    telehealth_api_enabled,
)
from app.models import TelehealthProvider, TelehealthSession, User
from app.telehealth import (
    _normalize_gender,
    _validate_dob,
    _validate_email,
    missing_patient_fields,
    split_full_name,
    sync_local_provider_from_remote_doctor,
)
from app.util import new_id, now_iso


class FeeParsingTests(unittest.TestCase):
    def test_parses_decimal_string(self):
        # Confirmed live: the API returns fees as "10000.00", not a bare int.
        self.assertEqual(_parse_fee("10000.00"), 10000)

    def test_parses_int(self):
        self.assertEqual(_parse_fee(5000), 5000)

    def test_none_or_bad_value_defaults_to_zero(self):
        self.assertEqual(_parse_fee(None), 0)
        self.assertEqual(_parse_fee("not-a-number"), 0)


class ListDoctorsParsingTests(unittest.TestCase):
    def setUp(self):
        invalidate_doctors_cache()
        self.env_patch = patch.dict(
            "os.environ",
            {
                "HC_TELEHEALTH_API_BASE_URL": "https://telehealthapi.karevault.com",
                "HC_TELEHEALTH_WHATSAPP_TOKEN": "test-token",
            },
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        invalidate_doctors_cache()

    def test_enabled_when_token_set(self):
        self.assertTrue(telehealth_api_enabled())

    def test_disabled_without_token(self):
        with patch.dict("os.environ", {"HC_TELEHEALTH_WHATSAPP_TOKEN": ""}):
            self.assertFalse(telehealth_api_enabled())
        self.assertEqual(list_doctors(force_refresh=True), [])

    def test_null_specialty_defaults_to_general(self):
        # Confirmed live: doctor id 82 has specialty: null.
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {"id": 82, "name": "Oluchi Ugwuoke", "specialty": None, "consultation_fee": "15000.00"}
        ]
        with patch("app.karevault_telehealth_client.requests.get", return_value=resp):
            docs = list_doctors(force_refresh=True)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].specialty, "General")
        self.assertEqual(docs[0].consultation_fee, 15000)

    def test_http_error_serves_stale_cache_not_empty(self):
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = [
            {"id": 1, "name": "Dr. A", "specialty": "Cardiology", "consultation_fee": "5000.00"}
        ]
        with patch("app.karevault_telehealth_client.requests.get", return_value=ok_resp):
            first = list_doctors(force_refresh=True)
        self.assertEqual(len(first), 1)

        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.text = "server error"
        with patch("app.karevault_telehealth_client.requests.get", return_value=err_resp):
            second = list_doctors(force_refresh=True)
        # Stale cache served, not blanked to [].
        self.assertEqual(second, first)


class ProviderSyncIdempotencyTests(unittest.TestCase):
    def setUp(self):
        with session_scope() as s:
            s.query(TelehealthProvider).filter(
                TelehealthProvider.external_doctor_id == "999999"
            ).delete()

    def tearDown(self):
        with session_scope() as s:
            s.query(TelehealthProvider).filter(
                TelehealthProvider.external_doctor_id == "999999"
            ).delete()

    def test_repeat_sync_updates_same_row(self):
        doctor_v1 = RemoteDoctor(id=999999, name="Dr. Test One", specialty="Cardiology", consultation_fee=5000)
        doctor_v2 = RemoteDoctor(id=999999, name="Dr. Test One Updated", specialty="Cardiology", consultation_fee=7000)

        with session_scope() as s:
            row1 = sync_local_provider_from_remote_doctor(s, doctor_v1)
            row1_id = row1.id

        with session_scope() as s:
            row2 = sync_local_provider_from_remote_doctor(s, doctor_v2)
            self.assertEqual(row2.id, row1_id)
            self.assertEqual(row2.full_name, "Dr. Test One Updated")
            self.assertEqual(row2.fee_ngn, 7000)

        with session_scope() as s:
            count = (
                s.query(TelehealthProvider)
                .filter(TelehealthProvider.external_doctor_id == "999999")
                .count()
            )
        self.assertEqual(count, 1)


class PatientInfoValidationTests(unittest.TestCase):
    def test_email_validation(self):
        self.assertEqual(_validate_email("name@example.com"), "name@example.com")
        self.assertIsNone(_validate_email("not-an-email"))
        self.assertIsNone(_validate_email(""))

    def test_gender_normalization(self):
        self.assertEqual(_normalize_gender("Male"), "male")
        self.assertEqual(_normalize_gender("f"), "female")
        self.assertEqual(_normalize_gender("Other"), "other")
        self.assertIsNone(_normalize_gender("banana"))

    def test_dob_validation(self):
        self.assertEqual(_validate_dob("1990-05-14"), "1990-05-14")
        self.assertIsNone(_validate_dob("not-a-date"))
        self.assertIsNone(_validate_dob("2999-01-01"))  # future date rejected

    def test_missing_patient_fields(self):
        self.assertEqual(sorted(missing_patient_fields(None)), ["dob", "email", "gender"])
        user = User(
            id="usr_test",
            phone_e164="+2340000000001",
            channel="whatsapp",
            language="en",
            email="a@b.com",
            gender="male",
            dob=None,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self.assertEqual(missing_patient_fields(user), ["dob"])

    def test_split_full_name(self):
        self.assertEqual(split_full_name("John Doe"), ("John", "Doe"))
        self.assertEqual(split_full_name("Madonna"), ("Madonna", ""))
        self.assertEqual(split_full_name(None), ("Patient", ""))
        self.assertEqual(split_full_name(""), ("Patient", ""))


class PaymentSequencingTests(unittest.TestCase):
    """create_appointment must not be called while a fee-bearing booking is still unpaid,
    and must be called exactly once once payment is confirmed."""

    PHONE = "+2340000000555"

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        with session_scope() as s:
            s.query(TelehealthSession).filter(
                TelehealthSession.patient_phone_e164 == self.PHONE
            ).delete()
            s.query(TelehealthProvider).filter(
                TelehealthProvider.external_doctor_id == "888888"
            ).delete()
            s.query(User).filter(User.phone_e164 == self.PHONE).delete()

    def _make_synced_provider_and_session(self, *, fee_ngn: int) -> str:
        from app.karevault_telehealth_client import RemoteDoctor

        with session_scope() as s:
            doctor = RemoteDoctor(id=888888, name="Dr. Fee Test", specialty="Cardiology", consultation_fee=fee_ngn)
            provider = sync_local_provider_from_remote_doctor(s, doctor)
            user = User(
                id=new_id("usr"),
                phone_e164=self.PHONE,
                channel="whatsapp",
                language="en",
                email="a@b.com",
                gender="male",
                dob="1990-01-01",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            s.add(user)
            s.flush()
            session_id = new_id("ths")
            s.add(
                TelehealthSession(
                    id=session_id,
                    user_id=user.id,
                    patient_phone_e164=self.PHONE,
                    provider_id=provider.id,
                    channel="whatsapp",
                    profession_snapshot=provider.profession,
                    status="pending_payment" if fee_ngn > 0 else "confirmed",
                    fee_ngn=fee_ngn,
                    currency="NGN",
                    billing_status="pending",
                    appointment_date="2026-09-01",
                    appointment_time="09:30",
                    call_mode="whatsapp_video",
                    mask_patient_number=True,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                    completed_at=None,
                )
            )
        return session_id

    def test_fee_bearing_booking_does_not_call_create_appointment_before_payment(self):
        # create_appointment / telehealth_api_enabled / is_configured are all imported
        # locally inside the functions that use them (matching this codebase's convention
        # for lazily-imported integrations), so the source module must be patched, not
        # app.telehealth's local names.
        session_id = self._make_synced_provider_and_session(fee_ngn=5000)
        with patch("app.karevault_telehealth_client.create_appointment") as mock_create, patch(
            "app.karevault_telehealth_client.telehealth_api_enabled", return_value=True
        ), patch("app.paystack_billing.is_configured", return_value=False), patch(
            "app.telehealth._dispatch_booking_notifications"
        ):
            from app.telehealth import finalize_new_booking

            finalize_new_booking(session_id)
        mock_create.assert_not_called()

    def test_payment_confirmed_calls_create_appointment_once(self):
        session_id = self._make_synced_provider_and_session(fee_ngn=5000)
        from app.karevault_telehealth_client import AppointmentResult

        with patch(
            "app.karevault_telehealth_client.create_appointment",
            return_value=AppointmentResult(ok=True, appointment_id=123, status="paid", call_url="https://x/call/123"),
        ) as mock_create, patch(
            "app.karevault_telehealth_client.telehealth_api_enabled", return_value=True
        ), patch("app.telehealth._notify_operator_telehealth_event"), patch(
            "app.telehealth.notify_helpdesk_telehealth_booking"
        ), patch("app.telehealth.deliver_post_payment_patient_message", return_value=(True, "ok")):
            from app.telehealth import on_payment_confirmed

            on_payment_confirmed(session_id, reference="ref123")

        mock_create.assert_called_once()
        with session_scope() as s:
            session = s.get(TelehealthSession, session_id)
            self.assertEqual(session.external_appointment_id, "123")
            self.assertEqual(session.external_call_url, "https://x/call/123")

    def test_waived_booking_calls_create_appointment_immediately(self):
        session_id = self._make_synced_provider_and_session(fee_ngn=0)
        from app.karevault_telehealth_client import AppointmentResult

        with patch(
            "app.karevault_telehealth_client.create_appointment",
            return_value=AppointmentResult(ok=True, appointment_id=456, status="paid", call_url=None),
        ) as mock_create, patch(
            "app.karevault_telehealth_client.telehealth_api_enabled", return_value=True
        ), patch("app.telehealth._dispatch_booking_notifications"), patch(
            "app.telehealth.attempt_whatsapp_call_for_session", return_value=(False, None)
        ):
            from app.telehealth import finalize_new_booking

            finalize_new_booking(session_id)

        mock_create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
