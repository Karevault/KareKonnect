import unittest

from app import ehr_client, triage


class IntakeStateMachineTests(unittest.TestCase):
    def test_initial_symptom_is_held_until_age_group_is_captured(self):
        stage, profile, reply, complaint = triage._advance_intake_state(
            stage="ask_age_group",
            profile={},
            response_text="I have a bad headache",
        )

        self.assertEqual(stage, "ask_age_group")
        self.assertEqual(profile.get("pending_complaint"), "I have a bad headache")
        self.assertIn("age group", reply or "")
        self.assertIsNone(complaint)

    def test_pending_complaint_is_released_after_context_answer(self):
        stage, profile, reply, complaint = triage._advance_intake_state(
            stage="ask_patient_context",
            profile={"age_group": "18_49", "pending_complaint": "I have a bad headache"},
            response_text="4",
        )

        self.assertEqual(stage, "complete")
        self.assertEqual(profile.get("patient_context"), "adult_man")
        self.assertIsNone(reply)
        self.assertEqual(complaint, "I have a bad headache")

    def test_maternal_program_removed_when_intake_says_not_pregnancy_related(self):
        programs = triage.assessment_program_context(
            programs=["maternal_child", "chronic"],
            triage_session_id=None,
        )

        self.assertEqual(programs, ["maternal_child", "chronic"])

        filtered = triage._advance_intake_state(
            stage="ask_patient_context",
            profile={"age_group": "18_49"},
            response_text="4",
        )
        _, profile, _, _ = filtered
        context_programs = [p for p in ["maternal_child", "chronic"] if p != "maternal_child"]
        self.assertEqual(profile.get("patient_context"), "adult_man")
        self.assertEqual(context_programs, ["chronic"])


class LocalRiskFactorTests(unittest.TestCase):
    def test_local_risk_factors_do_not_assume_pregnancy_from_program_only(self):
        self.assertEqual(ehr_client._local_risk_factors(["maternal_child"]), [])
        self.assertEqual(ehr_client._local_risk_factors(["chronic", "kidney"]), ["chronic_disease", "kidney_disease"])


if __name__ == "__main__":
    unittest.main()