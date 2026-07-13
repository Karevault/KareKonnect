"""Shared choice lists for HTML forms and SQLAdmin."""

from __future__ import annotations

CHANNEL_CHOICES = [
    ("sms", "SMS"),
    ("whatsapp", "WhatsApp"),
    ("ussd", "USSD"),
    ("web", "Web"),
]

LANGUAGE_CHOICES = [
    ("en", "English"),
    ("pcm", "Pidgin (Naija)"),
    ("ha", "Hausa"),
    ("ig", "Igbo"),
    ("yo", "Yoruba"),
]

PROGRAM_CHOICES = [
    ("maternal_child", "Maternal & child health"),
    ("chronic", "Chronic diseases"),
    ("youth", "Youth health"),
    ("kidney", "Dialysis & kidney"),
]

BROADCAST_PROGRAM_CHOICES = [("all", "All programmes")] + PROGRAM_CHOICES

BROADCAST_CHANNEL_CHOICES = [
    ("", "SMS and WhatsApp"),
    ("whatsapp", "WhatsApp only"),
    ("sms", "SMS only"),
]

BROADCAST_LANGUAGE_CHOICES = [("", "Any language")] + LANGUAGE_CHOICES

ENROLLMENT_STATUS_CHOICES = [
    ("active", "Active"),
    ("paused", "Paused"),
    ("completed", "Completed"),
]

OUTBOX_STATUS_CHOICES = [
    ("queued", "Queued"),
    ("sent", "Sent"),
    ("failed", "Failed"),
]

HELPDESK_STATUS_CHOICES = [
    ("open", "Open"),
    ("answered", "Answered"),
    ("escalated", "Escalated"),
    ("closed", "Closed"),
]

APPOINTMENT_STATUS_CHOICES = [
    ("requested", "Requested"),
    ("booked", "Booked"),
    ("cancelled", "Cancelled"),
]

APPOINTMENT_VISIT_TYPE_CHOICES = [
    ("in_person", "Clinic visit"),
    ("telehealth", "Video consultation"),
]

APPOINTMENT_EHR_SYNC_CHOICES = [
    ("pending", "Pending"),
    ("synced", "Synced"),
    ("failed", "Failed"),
    ("skipped", "Skipped"),
]

ESCALATION_URGENCY_CHOICES = [
    ("routine", "Routine"),
    ("urgent", "Urgent"),
    ("emergency", "Emergency"),
]

ESCALATION_STATUS_CHOICES = [
    ("requested", "Requested"),
    ("in_progress", "In progress"),
    ("resolved", "Resolved"),
]

RPM_STATUS_CHOICES = [
    ("requested", "Requested"),
    ("enrolled", "Enrolled"),
    ("declined", "Declined"),
]

TELEHEALTH_PROFESSION_CHOICES = [
    ("gynaecologist", "Gynaecologist"),
    ("dialysis", "Dialysis / Nephrology"),
    ("general_practice", "General practice"),
    ("cardiology", "Cardiology"),
    ("paediatrics", "Paediatrics"),
    ("psychiatry", "Mental health"),
    ("dermatology", "Dermatology"),
    ("ophthalmology", "Ophthalmology"),
    ("other", "Other specialist"),
]

TELEHEALTH_PROVIDER_STATUS_CHOICES = [
    ("draft", "Draft (hidden from patients)"),
    ("active", "Active (listed to patients)"),
    ("inactive", "Inactive"),
]

TELEHEALTH_SESSION_STATUS_CHOICES = [
    ("confirmed", "Confirmed"),
    ("in_progress", "In progress"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
    ("no_show", "No show"),
]

TELEHEALTH_BILLING_STATUS_CHOICES = [
    ("pending", "Pending payment"),
    ("paid", "Paid"),
    ("waived", "Waived"),
    ("failed", "Payment failed"),
    ("refunded", "Refunded"),
]

TELEHEALTH_CALL_MODE_CHOICES = [
    ("whatsapp_video", "WhatsApp video"),
    ("phone", "Phone call"),
    ("other", "Other"),
]
