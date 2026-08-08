"""Regression tests: completed assessments must not be truncated or replayed."""

from __future__ import annotations

from app.inbound_chat import _KAREVAULT_REPLY_MAX_LEN, _truncate_reply
from app.karevault_client import assessment_is_complete, build_contextual_question
from app.services import _is_answer_like


COMPLETED_ASSESSMENT = (
    "Dr. KareVault: After analyzing your symptoms through strategic questioning, I have "
    "completed your medical assessment.\n\n"
    "*CONFIRMED SYMPTOMS:*\n* Swelling\n* face\n\n"
    "*MEDICAL INFORMATION:*\n* Duration: yesterday recent\n\n"
    "*DIFFERENTIAL DIAGNOSIS:*\n* Pre-eclampsia\n* Nephrotic syndrome\n\n"
    "*PRIMARY DIAGNOSIS:*\nSuspected pre-eclampsia\n"
    "Reasoning: Facial swelling with mild abdominal pain in pregnancy.\n\n"
    "*RECOMMENDED NEXT STEPS:*\nAttend a facility today for blood pressure and urine protein checks."
)


def test_full_assessment_is_not_truncated() -> None:
    """A complete assessment fits well inside the limit, so no ellipsis is added."""
    out = _truncate_reply(COMPLETED_ASSESSMENT, _KAREVAULT_REPLY_MAX_LEN)
    assert out is not None
    assert not out.endswith("…")
    assert "RECOMMENDED NEXT STEPS" in out
    assert "Suspected pre-eclampsia" in out


def test_truncation_limit_leaves_room_within_whatsapp_cap() -> None:
    """WhatsApp caps text bodies at 4096 chars; the limit must stay under that."""
    assert 2000 <= _KAREVAULT_REPLY_MAX_LEN <= 3500


def test_truncation_still_applies_to_oversized_replies() -> None:
    out = _truncate_reply("x" * (_KAREVAULT_REPLY_MAX_LEN + 500), _KAREVAULT_REPLY_MAX_LEN)
    assert out is not None
    assert out.endswith("…")
    assert len(out) == _KAREVAULT_REPLY_MAX_LEN


def test_completed_assessment_is_detected() -> None:
    assert assessment_is_complete(COMPLETED_ASSESSMENT) is True


def test_intermediate_question_is_not_treated_as_complete() -> None:
    assert (
        assessment_is_complete(
            "Dr. KareVault: Are you experiencing any abdominal pain or cramping, "
            "and if so, is it severe or mild?"
        )
        is False
    )
    assert assessment_is_complete("") is False
    assert assessment_is_complete(None) is False


def test_new_consultation_prompt_asks_for_a_reset() -> None:
    fresh = build_contextual_question("my face is swollen", [], new_consultation=True)
    assert "NEW consultation" in fresh
    assert "Discard" in fresh

    ongoing = build_contextual_question("my face is swollen", [], new_consultation=False)
    assert "NEW consultation" not in ongoing


def test_short_answers_do_not_match_faqs() -> None:
    """"No" / "Thank you" answered an assessment question — they are not FAQ lookups."""
    for reply in ("No", "no", "Yes", "yeah", "Thank you", "thanks", "Hi", "ok", "None", "mild"):
        assert _is_answer_like(reply) is True, reply


def test_real_symptom_descriptions_still_match_faqs() -> None:
    for message in (
        "My vagina bled yesterday, but it was not heavy",
        "I have danger signs in pregnancy",
        "I need an appointment",
    ):
        assert _is_answer_like(message) is False, message
