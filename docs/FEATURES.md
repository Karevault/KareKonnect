# Kareconnect — Feature Guide

**Kareconnect** (HealthConnect) is a multi-channel patient engagement and clinical support platform for Nigerian healthcare programmes. Patients interact via **WhatsApp**, **SMS**, or **USSD**; staff operate through a **web admin console** and optional **REST APIs / webhooks**.

**Production:** [https://kareconnect.karevault.com](https://kareconnect.karevault.com)  
**Admin:** `/admin`  
**EHR API reference:** [https://ehr.karevault.com/api/docs](https://ehr.karevault.com/api/docs)

---

## Table of contents

1. [How patients join](#1-how-patients-join)
2. [Health programmes & education](#2-health-programmes--education)
3. [Messaging & languages](#3-messaging--languages)
4. [Symptom checker & clinical triage](#4-symptom-checker--clinical-triage)
5. [WhatsApp media intake](#5-whatsapp-media-intake)
6. [Emergency & clinical action paths](#6-emergency--clinical-action-paths)
7. [Telehealth & payments](#7-telehealth--payments)
8. [Helpdesk & escalations](#8-helpdesk--escalations)
9. [Appointments & remote monitoring](#9-appointments--remote-monitoring)
10. [Broadcasts](#10-broadcasts)
11. [EHR integration (KareVault)](#11-ehr-integration-karevault)
12. [Patient commands (keywords)](#12-patient-commands-keywords)
13. [Admin console](#13-admin-console)
14. [APIs & webhooks](#14-apis--webhooks)
15. [External integrations](#15-external-integrations)
16. [Background jobs & health checks](#16-background-jobs--health-checks)
17. [Editable content (no code deploy)](#17-editable-content-no-code-deploy)

---

## 1. How patients join

### Web registration (`/` or `/join`)

Patients can enrol through a mobile-friendly web form:

- Full name and E.164 phone number
- Preferred channel: SMS or WhatsApp
- Programme language: English, Nigerian Pidgin, Hausa, Igbo, or Yoruba
- One or more programmes (see [§2](#2-health-programmes--education))
- Optional expected delivery date (EDD) for maternal programmes
- Optional opt-in for daily WhatsApp education tips

After enrolment, the patient receives a welcome message (translated to their language). On WhatsApp, a **72-hour clinical triage intake window** opens automatically.

**QR codes:** `/join/qr.png` (web join) and `/join/wa-qr.png` (WhatsApp deep link).

### Chat enrolment (`JOIN`)

Already-registered numbers can send `JOIN` on SMS or WhatsApp to enrol or update programmes without using the web form. Repeat joins for programmes that are already active are treated as no-ops.

### USSD enrolment

On Africa's Talking USSD, patients dial the configured short code and choose a programme from a menu:

1. Maternal & child (EDD capture)
2. Chronic diseases
3. Youth health
4. Dialysis & kidney

USSD menu text and dial code are configurable in admin.

### Privacy

Public privacy policy at `/privacy` (linked from the join page for Meta app review).

---

## 2. Health programmes & education

### Programmes

| Programme | Focus |
|-----------|--------|
| **Maternal & child** | Pregnancy and postpartum education timed to EDD/week |
| **Chronic diseases** | Ongoing condition management and lifestyle tips |
| **Youth health** | Adolescent and young-adult health topics |
| **Dialysis & kidney** | Kidney disease and dialysis support |

### Scheduled education messages

- Programme-specific tips are drawn from `app/content/program_messages.json`
- Messages are queued in an **outbox** with a due time and sent on the patient's preferred channel
- A background scheduler enqueues due messages every minute and delivers queued messages every minute
- Messages are **translated** to the patient's `LANG` setting before send

### Pause / resume

- `STOP` — pauses programme messages
- `START` — resumes programme messages

### Daily WhatsApp tips (optional)

- `REMINDERS YES` / `REMINDERS NO` — toggles at most **one programme tip per day** on WhatsApp only (not SMS)

---

## 3. Messaging & languages

### Outbound routing

- **WhatsApp:** Meta Cloud API (primary); Twilio WhatsApp as fallback
- **SMS:** Africa's Talking (if configured); otherwise Twilio

### Inbound channels

| Channel | How it works |
|---------|----------------|
| **Meta WhatsApp** | Webhook at `/webhooks/meta/whatsapp` — text, images, documents, audio, delivery receipts, call events |
| **Twilio** | `/webhooks/twilio/inbound` (SMS/WhatsApp), `/webhooks/twilio/status` (delivery) |
| **Africa's Talking SMS** | `/webhooks/africastalking/sms` |
| **Africa's Talking USSD** | `/webhooks/africastalking/ussd` |

### Languages

Patients set language with `LANG EN`, `LANG PCM`, `LANG HA`, `LANG IG`, or `LANG YO`.

| Code | Language |
|------|----------|
| `en` | English |
| `pcm` | Nigerian Pidgin |
| `ha` | Hausa |
| `ig` | Igbo |
| `yo` | Yoruba |

**Translation pipeline:**

- Inbound patient text can be normalised to English for FAQ, KareVault AI, and clinical engines
- All outbound replies are translated back to the patient's language
- **Pidgin** uses on-device Marian neural models; **Hausa/Igbo/Yoruba** use Google Cloud Translation
- Results are cached in the database to reduce latency and API cost
- Optional learning-sample capture for future model improvement

### WhatsApp voice (optional)

- **Inbound:** Voice notes are transcribed (Google Speech-to-Text) and processed like text
- **Outbound:** Patients who send `VOICE YES` can receive short voice-note replies (Google Text-to-Speech) on WhatsApp; `VOICE NO` opts out

### 24-hour WhatsApp session & templates

Proactive messages (broadcasts, stale outbox retries) outside Meta's 24-hour customer-care window use an approved **template message** (configurable in admin). In-session users receive free-form text.

---

## 4. Symptom checker & clinical triage

### Free-text health questions

When a patient sends a normal message (not a command), the system:

1. Tries **local FAQ** matching (`app/content/faqs.json`)
2. Optionally calls the **KareVault symptom checker API** (when enabled)
3. Runs **clinical intelligence** when enabled: urgency tier, risk level, protocol, medication safety, fraud signals, differential notes

Replies include patient-safe guidance plus any clinical assessment suffix.

### Post-JOIN triage window (WhatsApp)

After joining on WhatsApp, patients enter a **clinical intake window** (default 72 hours, `HC_TRIAGE_WINDOW_HOURS`):

- Capture a **chief complaint** from free text
- Accept **photos and documents** for OCR and vision analysis (see [§5](#5-whatsapp-media-intake))
- `TRIAGE` — shows window status, complaint summary, and linked telehealth session if any

### Clinical assessments

Each assessment is stored with:

- Urgency: emergency / urgent / routine
- Risk: high / moderate / low
- Protocol ID (e.g. glucose monitoring, maternal danger signs, kidney care)
- Medication safety and fraud signals
- Actions taken (escalation, EHR push, teleconsult booking, etc.)

Assessments are visible in admin under **Clinical triage → Triage assessments**.

### Configurable action paths

Clinical behaviour for each tier and protocol is driven by `app/content/clinical_action_paths.json` — no code change required to adjust which actions fire.

---

## 5. WhatsApp media intake

During triage (or telehealth), patients can send on WhatsApp:

- **Images** — wounds, rashes, eyes, glucose meter screens, etc.
- **Documents** — lab results, prescriptions (PDF)

The system:

1. Downloads and stores the file (deduplicated by hash) under `data/attachments/`
2. Runs **Google Vision OCR** for text extraction
3. Runs **vision analysis** (labels, safe-search, category hints)
4. Feeds OCR + vision into the clinical intelligence pipeline
5. Can notify the assigned telehealth provider that media is available

Admin: **Clinical triage → Media attachments** (OCR excerpt, summary, link to patient).

---

## 6. Emergency & clinical action paths

When clinical intelligence flags **emergency** urgency, an automated action path runs:

1. **Patient instructions** — call national emergency line (default 122) or 112; go to hospital
2. **Auto-escalation** — emergency escalation record for operators
3. **On-call clinician alert** — WhatsApp/SMS to `HC_ON_CALL_CLINICIAN_PHONE`
4. **Next-of-kin notification** — separate message to each emergency contact registered for **that patient** (local admin or synced from EHR)
5. **EHR incident log** — pushed to KareVault when API keys are configured
6. **Urgent teleconsult slot** — emergency-priority telehealth session opened

### Glucose monitoring path (chronic programme)

When a glucose reading is detected (OCR from photo or text):

- Reading stored locally; trend merged with EHR history
- **30-day trend analysis** (stable, rising, elevated post-meal, hypoglycemia risk)
- **Medication regimen** fetched from EHR; **clinician approval draft** created
- **Dietary guidance** sent to patient
- **Follow-up teleconsult** booked (routine priority)
- **Reading reminder** scheduled in the outbox
- Observation pushed to EHR

Admin: **Glucose readings**, **Medication drafts**, **Clinical reminders**.

---

## 7. Telehealth & payments

### Booking flow (`TELEHEALTH`)

Interactive WhatsApp/SMS menu:

1. Patient sends `TELEHEALTH`
2. Chooses a **specialty** (e.g. general practice, cardiology, gynaecology, dialysis)
3. Sees **providers** with consultation fee and average rating
4. Confirms with `YES` → session created
5. Receives **OPay payment link** (if fee > 0), waived-fee instructions, or call booking details

Navigation: `BACK`, `CANCEL`, `EXIT`.

### Provider management (admin)

Staff register providers with:

- Name, specialty, phone, WhatsApp number
- Consultation fee (NGN)
- Active/inactive status
- Masked phone display for patient privacy

### OPay billing

- Hosted Cashier checkout URL generated per session
- Webhook `/webhooks/opay` on a `SUCCESS` payment callback marks session paid and notifies patient + provider
- Admin can manually mark a session paid (triggers post-payment message)

### Meta WhatsApp video calling

After payment (or waived fee) on WhatsApp:

- Optional **business-initiated video call** via Meta Calling API
- May use a WebRTC bridge URL for SDP exchange (`HC_META_WA_CALLING_WEBRTC_BRIDGE_URL`)
- Call status updates via Meta webhook

See also: `whatsapp-voice-integration.md` in the repo root.

### Post-visit ratings

`RATE 1-5 <optional comment>` — rates the most recent completed telehealth visit; updates provider average rating.

### Operator notifications

Optional webhook (`HC_OPERATOR_WEBHOOK_URL`) fires on `telehealth.session.booked` and `telehealth.session.paid`.

---

## 8. Helpdesk & escalations

### Helpdesk tickets

Patients open a ticket by starting a message with **Helpdesk** (e.g. `Helpdesk I need a callback`):

- FAQ suggestion attached when a match exists
- Staff at `HC_HELPDESK_PHONE` receive an alert with ticket ID
- Staff reply with `TKT <ticket_id> <message>` on the same channel; reply is translated to the patient's language
- Optional operator webhook on `helpdesk.ticket.created`

Admin: **Helpdesk** ticket list (open / answered / escalated / closed).

### Symptom checker shortcut

`CHECK` or `SYMPTOMS` — short instructions for free-text health questions vs helpdesk.

### Escalations

| Method | Usage |
|--------|--------|
| `ESCALATE ROUTINE <reason>` | Callback request |
| `ESCALATE URGENT <reason>` | Urgent callback |
| `ESCALATE EMERGENCY <reason>` | Emergency — also use hospital/122 for life-threatening cases |
| `POST /api/escalations` | Programmatic escalation |

Clinical emergency paths also create escalations automatically.

Admin: **Telehealth Escalations**.

---

## 9. Appointments & remote monitoring

### Appointments

- Chat: `APPOINT <reason>` (e.g. `APPOINT antenatal`)
- API: `POST /api/appointments`

Creates a request for staff follow-up. Admin: **Appointments**.

### Remote patient monitoring (RPM)

- Chat: `RPM <condition>` (e.g. `RPM hypertension`)
- API: `POST /api/rpm/enroll`

Creates an RPM enrollment request (requested / enrolled / declined). Admin: **RPM**.

---

## 10. Broadcasts

Staff can send **one-off campaigns** to filtered audiences from admin (**Messaging → Broadcast**):

**Filters:** programme, enrollment status, channel, language  
**Preview:** audience count, WhatsApp in-session vs template-required breakdown  
**Options:** translate body per recipient language  
**Delivery:** immediate send + periodic retry for stale runs  
**Outside 24h WA window:** approved Meta template path  
**History:** per-run delivery status per recipient

---

## 11. EHR integration (KareVault)

When `HC_EHR_PARTNER_API_KEY` and `HC_EHR_TELECONSULT_API_KEY` are set (see `.env.example`):

### Patient identity (Partner API)

On JOIN and before clinical sync:

1. **Lookup** patient by `phone_e164`
2. If not found, **create** patient (idempotent by phone / Kareconnect `user_id`)
3. Store link in `ehr_patient_links` (`user_id` ↔ EHR `patients.id`)
4. Import **next-of-kin** from EHR when patient has no local contacts

### Clinical reads (WhatsApp Teleconsult API)

- Patient context — demographics, diagnoses, allergies, medications, glucose, vitals
- Glucose history (30-day window, up to 365 days)
- Active medications
- Next-of-kin list

### Clinical writes

- **Incidents** — emergency triage, operational events
- **Observations** — triage assessments, glucose readings (mirrored to vitals when configured)

### Per-patient next of kin

- Each patient has their own contact list (`patient_next_of_kin`)
- Manage via admin or `POST/GET /api/patients/.../next-of-kin`
- Emergency path notifies each contact individually

### Audit

All EHR API calls logged in **EHR sync logs**; manual links in **EHR patient links**.

---

## 12. Patient commands (keywords)

Configurable in admin (**Channel Settings**). Default behaviour:

| Command | What it does |
|---------|----------------|
| `INFO` / `MENU` / `COMMANDS` | Full help menu |
| `CHECK` / `SYMPTOMS` | Symptom-checker instructions |
| `JOIN` | Enrol from chat |
| `STOP` / `START` | Pause / resume programme messages |
| `REMINDERS YES` / `NO` | Daily WhatsApp tips |
| `VOICE YES` / `NO` | WhatsApp voice replies |
| `LANG EN\|PCM\|HA\|IG\|YO` | Set language |
| `APPOINT <reason>` | Appointment request |
| `TELEHEALTH` | Book video consultation |
| `TRIAGE` | Triage intake status |
| `RATE 1-5 <comment>` | Rate telehealth visit |
| `ESCALATE ROUTINE\|URGENT\|EMERGENCY <reason>` | Escalation |
| `RPM <condition>` | RPM request |
| `Helpdesk <question>` | Open helpdesk ticket |
| *(free text)* | FAQ + symptom checker + clinical triage |
| `TKT <id> <message>` | **Staff only** — reply to helpdesk ticket |

---

## 13. Admin console

**URL:** `/admin` (login required; bcrypt users in `admin_users`)

### Core operations

| Area | What you can do |
|------|-----------------|
| **Users & enrollments** | View/edit patients and programme membership |
| **Outbox** | Inspect and manually queue messages |
| **Inbound messages** | Full inbound audit log |
| **Helpdesk / appointments / escalations / RPM** | Operate request queues |

### Messaging

| Area | What you can do |
|------|-----------------|
| **Broadcast** | Compose, preview, send campaigns |
| **Broadcast history** | Per-recipient delivery outcomes |

### Telehealth

| Area | What you can do |
|------|-----------------|
| **Providers** | Register clinicians, fees, status |
| **Sessions** | Bookings, billing, call status; mark paid |
| **Ratings** | Post-visit patient ratings |

### Clinical triage

| Area | What you can do |
|------|-----------------|
| **Triage sessions** | Intake windows and chief complaints |
| **Media attachments** | OCR and vision results |
| **Triage assessments** | Clinical intelligence output |
| **Next of kin (per patient)** | Emergency contacts |
| **Glucose readings** | OCR and trend data |
| **Medication drafts** | Clinician approval queue |
| **Clinical reminders** | Scheduled follow-ups |

### EHR integration

| Area | What you can do |
|------|-----------------|
| **EHR patient links** | Kareconnect user ↔ EHR ID |
| **EHR sync logs** | API request/response audit |

### Translation & ML

| Area | What you can do |
|------|-----------------|
| **Monitoring overview** | Cache, learning samples, live Pidgin test |
| **Voice message logs** | STT/TTS audit |
| **Learning samples / cache** | Training data and memoization |

### Setup & settings

| Area | What you can do |
|------|-----------------|
| **Test chat** | Simulate patient messages without webhooks |
| **Webhooks reference** | Copy webhook URLs; run connectivity self-tests |
| **Deployment & integrations** | Production URLs, Meta/Twilio/AT/OPay/EHR credentials |
| **Channel settings** | Keywords, USSD text, help menu, voice toggles |
| **Twilio delivery events** | Delivery receipt log |

### Integration self-tests

Hidden admin routes smoke-test Meta Graph API, Twilio auth, Africa's Talking, and broadcast template configuration.

---

## 14. APIs & webhooks

### REST APIs (partner / integration)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/ussd/session` | JSON USSD step (non-AT gateways) |
| POST | `/api/escalations` | Create escalation |
| POST | `/api/appointments` | Create appointment |
| POST | `/api/rpm/enroll` | RPM enrollment |
| POST | `/api/ehr/link` | Manual EHR patient link |
| POST | `/api/ehr/ensure-patient` | Lookup or create EHR patient |
| POST | `/api/ehr/patient-context` | Merged clinical context |
| POST | `/api/patients/next-of-kin` | Add emergency contact |
| GET | `/api/patients/{user_id}/next-of-kin` | List emergency contacts |
| POST | `/v1/detect` | Language detection (Bearer auth) |
| POST | `/v1/translate` | Marian Pidgin translation (Bearer auth) |

### Inbound webhooks

| Endpoint | Provider |
|----------|----------|
| `GET/POST /webhooks/meta/whatsapp` | Meta WhatsApp |
| `POST /webhooks/twilio/inbound` | Twilio SMS/WhatsApp |
| `POST /webhooks/twilio/status` | Twilio delivery |
| `POST /webhooks/africastalking/sms` | AT SMS |
| `POST /webhooks/africastalking/ussd` | AT USSD |
| `POST /webhooks/opay` | OPay payments |

### Outbound operator webhooks

JSON POST to `HC_OPERATOR_WEBHOOK_URL` for helpdesk and telehealth events.

---

## 15. External integrations

| Service | Role |
|---------|------|
| **Meta WhatsApp Cloud API** | Primary WhatsApp, media, templates, calling |
| **Twilio** | SMS/WhatsApp fallback, delivery callbacks |
| **Africa's Talking** | SMS and USSD |
| **Google Cloud Translation** | Hausa, Igbo, Yoruba; detect language |
| **Google Cloud Vision** | OCR and image analysis |
| **Google Speech / TTS** | WhatsApp voice notes |
| **Marian NMT + FastText** | Pidgin translation and language ID (local models) |
| **KareVault symptom checker** | AI symptom replies |
| **KareVault EHR** | Patient records, clinical context, incidents, observations |
| **OPay** | Telehealth payments (NGN) |

Credentials can be set via environment variables or overridden in admin (**Deployment & integrations**).

---

## 16. Background jobs & health checks

### Scheduler (every minute)

- Enqueue programme messages that are due
- Send queued outbox messages (with translation)
- Retry stale broadcast deliveries

### Health endpoints

| Endpoint | Checks |
|----------|--------|
| `GET /healthz` | App liveness |
| `GET /healthz/clinical` | Clinical intelligence, Vision OCR, EHR config, action paths |
| `GET /healthz/translation` | Pidgin Marian smoke test |
| `GET /healthz/voice` | STT/TTS configuration |
| `GET /healthz/messaging` | Outbound provider readiness |

---

## 17. Editable content (no code deploy)

| File | Controls |
|------|----------|
| `app/content/faqs.json` | FAQ answers for helpdesk and symptoms |
| `app/content/program_messages.json` | Scheduled programme education copy |
| `app/content/clinical_action_paths.json` | Clinical urgency/protocol actions |

Default help menu text: `app/default_help_menu.py` (also overridable in admin Channel Settings).

---

## Architecture at a glance

```
Patient (WhatsApp / SMS / USSD / Web)
        │
        ▼
   Kareconnect API  ──►  PostgreSQL (users, enrollments, outbox, clinical, telehealth)
        │
        ├──► Meta / Twilio / Africa's Talking  (messaging)
        ├──► Google Translate / Vision / Voice
        ├──► Marian ML (Pidgin)
        ├──► KareVault symptom checker
        ├──► KareVault EHR (Partner + Teleconsult APIs)
        ├──► OPay (telehealth billing)
        └──► Operator webhook (optional CRM)
```

---

*Last updated from codebase inventory — June 2026. For deployment and env vars, see `README.md` and `.env.example`.*
