"""Single source for the default SMS/WhatsApp help menu (Channel settings in admin)."""

DEFAULT_HELP_MENU_TEXT = (
    "Kareconnect — send one line at a time.\n\n"
    "Programmes: maternal & child, chronic disease, youth health, kidney care.\n\n"
    "Commands:\n"
    "• INFO / MENU / COMMANDS — show this list\n"
    "• JOIN — enrol  •  STOP — pause  •  START — resume\n"
    "• CHECK — health question reminder\n"
    "• LANG — language menu (EN, PCM, HA, IG, YO)\n"
    "• APPOINT <reason> — book appointment\n"
    "• TELEHEALTH — book video consult\n"
    "• TRIAGE — triage status\n"
    "• RATE 1-5 <comment> — rate your visit\n"
    "• ESCALATE ROUTINE|URGENT|EMERGENCY <reason> — callback request\n"
    "• RPM <condition> — remote monitoring\n"
    "• RESET / START OVER — exit current flow\n"
    "• REMINDERS YES|NO — daily WhatsApp tips  •  VOICE YES|NO — voice replies\n\n"
    "Health questions: reply in your own words (e.g. \"bad headache 2 days\"). "
    "On WhatsApp, send photos or documents with a caption.\n\n"
    "Helpdesk: start your message with Helpdesk — e.g. Helpdesk I need a callback."
)

DEFAULT_SYMPTOM_CHECK_REPLY = (
    "Health questions & symptom checker:\n"
    "Reply in your own words — not a command — for example \"dizzy and nauseous\" or \"lower back pain\". "
    "We use our FAQ and symptom checker to suggest guidance.\n"
    "To reach our team, start a new message with Helpdesk — e.g. Helpdesk I need to speak to someone.\n"
    "Emergencies: go to the nearest facility or send ESCALATE EMERGENCY … — do not wait only for a text.\n"
    "Send INFO, MENU, or COMMANDS for the full keyword list."
)
