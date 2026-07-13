# Kareconnect — Features

## Patient access
- Web join form (`/join`)
- Join via QR code
- Join via WhatsApp deep link
- Enrol via SMS/WhatsApp (`JOIN`)
- Enrol via USSD menu
- Privacy policy page

## Health programmes
- Maternal & child health
- Chronic disease follow-up
- Youth health
- Dialysis & kidney care
- Scheduled education messages
- Pause/resume messages (`STOP` / `START`)
- Optional daily WhatsApp tips (`REMINDERS YES/NO`)

## Messaging
- WhatsApp (Meta Cloud API)
- SMS (Africa's Talking / Twilio)
- USSD (Africa's Talking)
- Outbound message queue & scheduler
- Delivery status tracking
- 24-hour WhatsApp session handling
- WhatsApp template messages for out-of-session sends

## Languages
- English, Nigerian Pidgin, Hausa, Igbo, Yoruba
- Inbound translation to English for processing
- Outbound translation to patient language
- Staff helpdesk replies translated to patient language


## WhatsApp voice
- Voice note transcription (inbound)
- Voice note replies (opt-in via `VOICE YES`)

## Symptom checker & triage
- Free-text health questions
- Local FAQ matching
- KareVault AI symptom checker
- Clinical intelligence (urgency, risk, protocol)
- Post-JOIN triage intake window (WhatsApp)
- Chief complaint capture
- `TRIAGE` status command
- Triage assessment history

## Media intake (WhatsApp)
- Photo upload (wounds, rashes, eyes, glucose meters, etc.)
- Document upload (labs, prescriptions)
- OCR text extraction
- Image vision analysis
- Clinical assessment from media

## Clinical action paths
- Emergency response (122/112 instructions, on-call alert)
- Per-patient next-of-kin emergency alerts
- EHR incident logging
- Urgent teleconsult booking
- Glucose reading capture (OCR)
- 30-day glucose trend analysis
- Medication adjustment drafts (clinician approval)
- Dietary guidance
- Glucose reading reminders

## Telehealth
- Book video consultation (`TELEHEALTH`)
- Specialty & provider selection
- Provider fees & ratings display
- Paystack payment links
- Payment webhook confirmation
- Meta WhatsApp video calling
- Post-visit ratings (`RATE 1-5`)
- Provider directory (admin)

## Helpdesk & requests
- Helpdesk tickets (`Helpdesk …`)
- FAQ suggestions on ticket open
- Staff reply via `TKT <id>` pattern
- Staff reply translation to patient language
- Escalations (`ESCALATE ROUTINE/URGENT/EMERGENCY`)
- Appointment requests (`APPOINT`)
- Remote monitoring requests (`RPM`)

## Broadcasts
- Audience filters (programme, channel, language, status)
- Audience preview & WhatsApp session stats
- Per-recipient language translation
- Broadcast history & delivery status

## EHR integration (KareVault)
- Patient lookup by phone
- Auto-create patient if missing
- Patient clinical context (diagnoses, meds, allergies, vitals)
- Glucose history from EHR
- Medication list from EHR
- Per-patient next-of-kin (local + EHR sync)
- Push incidents & observations to EHR
- EHR sync audit log

## Admin console
- User & enrollment management
- Message outbox
- Inbound message log
- Helpdesk, appointments, escalations, RPM queues
- Broadcast compose & history
- Telehealth providers, sessions, ratings
- Triage sessions, media, assessments
- Next of kin, glucose readings, medication drafts, reminders
- EHR patient links & sync logs
- Translation monitoring & cache
- Integration settings (Meta, Twilio, AT, Paystack, EHR)
- Channel settings (keywords, USSD, help menu)
- Test chat simulator
- Webhook reference & self-tests

## APIs & webhooks
- REST APIs (escalations, appointments, RPM, EHR, next-of-kin, USSD)
- Meta WhatsApp webhook
- Twilio inbound & status webhooks
- Africa's Talking SMS & USSD webhooks
- Paystack payment webhook
- Operator event webhooks (helpdesk, telehealth)
- Health check endpoints

## Integrations
- Meta WhatsApp Cloud API
- Twilio
- Africa's Talking
- Google Cloud Translation (HA/IG/YO + detect/translate fallback)
- Google Cloud Vision
- Google Speech / Text-to-Speech
- In-house FastText + Marian (Pidgin; see Translation & ML above)
- KareVault symptom checker
- KareVault EHR
- Paystack
