# KareVault Limited
## KARECONNECT WHATSAPP CHATBOT
### Remediation & Update Report — Response to QA Consolidated Testing & Usability Report (August 2026)

**Date:** 12 August 2026
**Prepared by:** Technology Team | KareVault Limited
**Submitted to:** Management & Strategic Operations
**Staging Environment:** https://healthconnect-api-staging.up.railway.app

---

## 1. Executive Summary

Following the Consolidated Testing & Usability Report dated 10 August 2026 (covering test period 8–9 August 2026), the technology team has completed a second remediation cycle targeting all 13 issues raised in the QA issue log. This report documents the fixes implemented, partial fixes with remaining work, and items that require external platform or API changes.

**Remediation at a glance:**

| Metric | Result |
|--------|--------|
| Total QA issues in report | 13 |
| Fully fixed (codebase) | 9 |
| Partially fixed (codebase improvements, external dependency remains) | 3 |
| Not fixable from codebase (platform/infrastructure) | 1 |
| Automated regression tests | 56 passing |
| New test classes added | 4 (KareVault prompt, lab extraction, help menu trim, booking confirmation) |

---

## 2. Fully Fixed Issues

### 2.1 Appointment Confirmation Now Recognizes YES/CONFIRM and Completes Booking
**QA Issue #1 — CRITICAL**

The confirmation step in both appointment and telehealth booking flows now recognizes the first alphanumeric token of the user's reply, matching against a synonym set: `YES`, `Y`, `YEAH`, `CONFIRM`, `BOOK`, `PROCEED`, `OK`, `SURE`, and more. Punctuation is stripped (`YES.`, `yes!` both work). Case-insensitive.

When the user's reply is not recognized as a confirmation, the bot re-prompts within the booking flow instead of falling through to the generic symptom handler. The booking API/database transaction fires on successful confirmation.

**Files changed:** `app/appointments.py`, `app/telehealth.py`
**Tests:** `tests/test_booking_confirmation.py` — 8 confirmation tests covering video booking, clinic booking, unrecognized re-prompts, and command escapes.

### 2.2 Conversation State No Longer Lost Mid-Flow
**QA Issue #2 — CRITICAL**

Active booking flows (appointment and telehealth) now take priority over the generic symptom/FAQ handler. The message router checks `is_in_appointment_flow` and `is_in_telehealth_flow` before any clinical processing. Unrecognized input within a flow re-prompts the user with flow-specific guidance instead of falling through.

Escape commands (`INFO`, `MENU`, `RESET`, `TELEHEALTH`, etc.) still work via `ROUTER_ESCAPE_COMMANDS` to let users exit a flow intentionally.

**Files changed:** `app/appointments.py`, `app/telehealth.py`, `app/inbound_chat.py`
**Tests:** `tests/test_booking_confirmation.py` — state retention and command escape tests.

### 2.3 Lab Results (PDF/Image) Now Feed Into Triage Assessment
**QA Issue #4 — HIGH**

OCR-extracted text from uploaded documents is now passed as `patient_text` to `run_clinical_assessment` (previously only the caption was passed). A new `_extract_lab_summary()` function regex-extracts key lab values (glucose, HbA1c, creatinine, cholesterol, LDL/HDL, triglycerides, urea, potassium, sodium, haemoglobin, WBC, platelets, blood pressure, pulse) from OCR text and surfaces them in the patient-facing reply.

**Files changed:** `app/media_intake.py`
**Tests:** `tests/test_booking_confirmation.py::LabSummaryExtractionTests` — 4 tests covering glucose, creatinine, empty text, and no-lab-values cases.

### 2.4 Consultation Requests Now Reach Booking
**QA Issue #6 — HIGH**

This was a symptom of the same root cause as Issues #1 and #2. With confirmation token matching and state management fixed, consultation requests now proceed through the full flow to booking completion without looping.

### 2.5 Complaints Routed to Helpdesk, Not Clinical Booking
**QA Issue #7 — HIGH**

A complaint-detection regex (`_COMPLAINT_RE`) now intercepts messages containing "complaint", "complain", "lodge a complaint", "report an issue", "dissatisfied", "unhappy with service", etc. These are routed to a helpdesk support prompt instead of dropping into medical specialty selection.

**Files changed:** `app/inbound_chat.py`

### 2.6 Bot Answers FAQ Questions Mid-Booking Flow
**QA Issue #8 — HIGH**

When a user in a booking flow sends a question-like message (starts with what/how/can/should/is/where/when/who, or ends with `?`) that does not contain symptom words, the bot checks FAQ matches first. If a match is found, the answer is delivered, then the booking flow re-prompts — so the user gets their question answered without losing their place in the flow.

**Files changed:** `app/inbound_chat.py`

### 2.7 Verbose Responses Trimmed
**QA Issue #9 — HIGH (verbosity component)**

- Help menu (`DEFAULT_HELP_MENU_TEXT`) condensed from ~1,800 to ~750 characters.
- Starter menu (`_starter_menu_text`) shortened — removed verbose language name listings and redundant explanations.
- Join welcome text (`_join_welcome_text`) condensed — WhatsApp extras reduced to one line.

**Files changed:** `app/default_help_menu.py`, `app/inbound_chat.py`
**Tests:** `tests/test_booking_confirmation.py::HelpMenuTrimTests` — 2 tests verifying conciseness and key command presence.

### 2.8 Initial Menu Trimmed
**QA Issue #11 — MODERATE**

The main help menu no longer includes lengthy explanations of RPM, FAQ matching internals, or symptom checker pipeline details. Each command is now a single concise line. The full health-questions explanation is reduced to two sentences. Helpdesk instructions are one line.

**Files changed:** `app/default_help_menu.py`

### 2.9 Duplicate Provider Titles Fixed
**QA Issue #13 — LOW**

A `format_provider_name()` helper now deduplicates provider titles. If the provider's stored name already starts with "Dr." (or "Dr"), the title prefix is not appended again. Applied consistently across appointment menus, telehealth menus, notifications, and confirmations.

**Files changed:** `app/appointments.py`, `app/telehealth.py`
**Tests:** `tests/test_booking_confirmation.py::ProviderNameFormattingTests` — 3 tests covering deduplication, title addition when missing, and no-title case.

---

## 3. Partially Fixed Issues (Codebase Improvements, External Dependency Remains)

### 3.1 Symptom Checker Returns Irrelevant Results
**QA Issue #3 — CRITICAL**

**What we fixed (codebase side):**
- Rewrote the KareVault contextual prompt to explicitly anchor the AI's reply to the patient's reported symptom.
- Added a conciseness directive ("Reply in 2-4 short sentences — practical and direct").
- Patient message is now delimited on its own line (`Patient message:\n…`) for clearer parsing by the API.
- New-consultation reset directive instructs the API to discard all prior context.

**What remains (external API dependency):**
The root cause of irrelevant triage results is the KareVault symptom checker API's NLU/mapping logic — the external service that turns free-text symptoms into triage categories. Our prompt improvements help guide the API, but if the API's matching engine itself maps "tiredness" to unrelated categories, that requires changes on the KareVault API side.

**What needs to be done for a complete fix:**
1. **KareVault API team** must review and improve the symptom-to-category NLU mapping in their backend, particularly for common general symptoms (fatigue, tiredness, weakness) that may not have strong keyword anchors.
2. **Expand the symptom test set** — test with 20+ diverse symptoms beyond the scripted demo cases to identify systematic mapping failures.
3. **Consider a fallback triage layer** — if the KareVault API response doesn't mention any keyword from the patient's original message, flag it as low-confidence and supplement with our internal clinical intelligence assessment.
4. **API session memory** — the KareVault API may benefit from maintaining conversation context server-side rather than relying solely on our prompt-level reset directives.

**Files changed:** `app/karevault_client.py`
**Tests:** `tests/test_booking_confirmation.py::KareVaultPromptTests` — 4 tests verifying prompt anchoring, conciseness, message delimiting, and reset directive.

### 3.2 Help Desk Does Not Connect to Live Support
**QA Issue #5 — HIGH**

**What we fixed (codebase side):**
- Helpdesk ticket replies now include a one-tap `wa.me` link (e.g., `https://wa.me/2348000000000?text=Follow%20up%20on%20ticket%20TKT-12345`) that opens a WhatsApp chat with the support team with the ticket reference prefilled in the message body.
- Users no longer need to manually copy, edit, and forward reference numbers.

**What remains (feature/infrastructure):**
The `wa.me` link opens a WhatsApp chat to the helpdesk number, but it does not create a real-time agent chat handoff within the same conversation thread. A true "live support" experience would require:
1. **Real-time agent routing** — a system where helpdesk agents can pick up conversations and reply inline within the same WhatsApp thread (requires a shared inbox / agent console integration).
2. **SLA tracking** — automated follow-up if no agent responds within a defined window.
3. **Agent availability detection** — route to available agents or queue messages when all agents are busy.

**What needs to be done for a complete fix:**
1. Integrate a shared inbox tool (e.g., Trengo, Respond.io, or a custom agent console) that allows support staff to see and reply to helpdesk conversations from within the same WhatsApp number.
2. Build or integrate an agent assignment and routing layer.
3. Add automated SLA monitoring (e.g., if no agent reply within 30 minutes, escalate via SMS to on-call staff).

**Files changed:** `app/inbound_chat.py`

### 3.3 Responses Feel Repetitive and Not Tailored
**QA Issue #10 — MODERATE**

**What we fixed (codebase side):**
- KareVault prompt now anchors to the specific symptom reported, encouraging tailored responses.
- Conciseness directive reduces boilerplate repetition.
- FAQ answers are now delivered mid-flow when users ask questions, reducing repetitive "you are in a booking" messages.

**What remains (external API dependency):**
True conversational memory — where the AI remembers what the user already told it and tailors follow-up questions accordingly — depends on the KareVault API maintaining session state. Our prompt-level reset directive helps for new consultations, but the API's ability to build on prior context within a consultation is an API-side feature.

**What needs to be done for a complete fix:**
1. **KareVault API** should implement server-side conversation memory, so follow-up questions reference prior answers without the patient repeating themselves.
2. **Context-aware triage** — the API should use the patient's age group, gender context, and programme enrolment (which we already pass) to tailor triage pathways.
3. **Response variation** — the API should avoid repeating identical assessment text across different consultations.

**Files changed:** `app/karevault_client.py`, `app/inbound_chat.py`

---

## 4. Not Fixable From Codebase (Platform/Infrastructure)

### 4.1 No Quick-Reply/Action Buttons
**QA Issue #12 — MODERATE**

WhatsApp quick-reply buttons and action buttons require the WhatsApp Business API's interactive message types (button templates, list templates, single-item list messages). Implementing these requires:

1. **Meta Business Manager approval** of interactive message templates.
2. **Template submission and review** — each button layout must be pre-approved by Meta.
3. **API integration** — switching from plain text messages to the interactive message payload format in the Meta WhatsApp Business API client.
4. **UI/UX design** — designing button layouts for each flow step (specialty selection, provider selection, confirmation, etc.).

This is a platform integration project, not a code patch, and should be scoped as a separate feature initiative.

---

## 5. Complete Issue Log — Remediation Status

| # | Issue | Severity | Status | Fix Reference |
|---|-------|----------|--------|---------------|
| 1 | Appointment confirmation not recognized | CRITICAL | **FIXED** | §2.1 |
| 2 | State lost mid-flow | CRITICAL | **FIXED** | §2.2 |
| 3 | Symptom checker irrelevant results | CRITICAL | **PARTIAL** | §3.1 |
| 4 | Lab results not used in triage | HIGH | **FIXED** | §2.3 |
| 5 | Help Desk doesn't connect to support | HIGH | **PARTIAL** | §3.2 |
| 6 | Consultation requests loop without booking | HIGH | **FIXED** | §2.4 |
| 7 | Complaints routed to clinical booking | HIGH | **FIXED** | §2.5 |
| 8 | Bot can't answer questions mid-flow | HIGH | **FIXED** | §2.6 |
| 9 | State lost + verbose responses | HIGH | **FIXED** | §2.2, §2.7 |
| 10 | Responses repetitive, not tailored | MODERATE | **PARTIAL** | §3.3 |
| 11 | Initial menu too wordy | MODERATE | **FIXED** | §2.8 |
| 12 | No quick-reply buttons | MODERATE | **NOT FIXABLE** | §4.1 |
| 13 | Duplicate provider titles | LOW | **FIXED** | §2.9 |

---

## 6. Verification & Test Coverage

All fixes are protected by an automated regression suite:

```
pytest — 56 passed
```

| Test File | Coverage |
|-----------|----------|
| `tests/test_booking_confirmation.py` | 24 tests: appointment/telehealth confirmation tokens, state retention, command escapes, provider name formatting, WhatsApp formatting, KareVault prompt, lab extraction, help menu trim |
| `tests/test_consultation_session_end.py` | Full diagnosis delivery, truncation limits, completion detection, FAQ bypass |
| `tests/test_consultation_ends_at_menu.py` | Session end at menu, one-time greeting, inactivity timeout |
| `tests/test_onboarding_intake.py` | Intake state machine, no pregnancy assumptions, symptom-holding |
| `tests/test_menu_reset.py` | RESET recovery menu, greeting → starter menu |
| `tests/test_context_language_routing.py` | Dietary guidance, service-scope answers, language switching |
| `tests/test_telehealth_booking.py` | Telehealth booking flow and payment status |
| `tests/test_ocr_extraction.py` | OCR text extraction for images and PDFs |

---

## 7. Recommended Next Steps

### For the partially fixed issues:

1. **Symptom Checker (#3):** Engage the KareVault API team to review their NLU/mapping logic. Expand testing to 20+ diverse symptoms. Consider implementing a confidence-check fallback in our codebase that supplements low-confidence API responses with our internal clinical intelligence assessment.
2. **Helpdesk Live Support (#5):** Evaluate shared inbox platforms (Trengo, Respond.io) for agent-console integration. Build SLA monitoring for unresponded tickets.
3. **Response Tailoring (#10):** Request KareVault API team implement server-side conversation memory for context-aware follow-up questions.

### For the platform-level issue:

4. **Quick-Reply Buttons (#12):** Scope as a separate feature initiative requiring Meta template approval and interactive message API integration.

### For QA:

5. **Retest all 9 fully fixed issues** using the exact reproduction steps from the August 2026 QA report, particularly the appointment confirmation flow across multiple specialty pathways (Gynaecology, General Practice).
6. **Test the partially fixed improvements** — verify that the KareVault prompt improvements produce more relevant responses for common symptoms (tiredness, headache, swollen feet).
7. **Test the helpdesk wa.me link** — verify the one-tap link opens WhatsApp with the correct prefilled message and ticket reference.

---

*KareVault Limited | 83, Ikotun-Idimu Road by Paradise Bus Stop, Ikotun, Lagos*
*www.karevault.com | info@karevault.com*
