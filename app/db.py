import logging

from sqlalchemy import text

from app.database import engine, session_scope
from app.default_help_menu import DEFAULT_HELP_MENU_TEXT
from app.models import AppSettings, Base
from app.util import now_iso


def _ensure_users_full_name_column() -> None:
    """create_all does not add columns on existing DBs."""
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255)")
        )


def _ensure_helpdesk_ticket_ai_column() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE helpdesk_tickets ADD COLUMN IF NOT EXISTS ai_symptom_reply TEXT"))


def _ensure_app_settings_symptom_check_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS symptom_check_keywords VARCHAR(255) "
                "DEFAULT 'CHECK,SYMPTOMS'"
            )
        )
        conn.execute(
            text(
                "UPDATE app_settings SET symptom_check_keywords = 'CHECK,SYMPTOMS' "
                "WHERE id = 1 AND (symptom_check_keywords IS NULL OR TRIM(symptom_check_keywords) = '')"
            )
        )


def _ensure_integration_settings_columns() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS hc_sms_join_number VARCHAR(32)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS helpdesk_phone_e164 VARCHAR(32)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS "
                "meta_wa_broadcast_template VARCHAR(128)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS "
                "meta_wa_broadcast_template_language VARCHAR(16)"
            )
        )


def _migrate_app_settings_twilio_reserved_help() -> None:
    """Move defaults off HELP (Twilio-reserved on many SMS lines) and refresh menu copy."""
    NEW_KW = "INFO,MENU,COMMANDS"
    OLD_MENU_PREFIX = "HealthConnect commands (reply with one line):"
    OLD_BULLET = "• HELP / INFO / MENU"
    old_kw_set = {
        "HELP,INFO,MENU",
        "HELP",
        "HELP,INFO",
        "HELP,MENU",
        "INFO,MENU,HELP",
    }

    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            return
        sk = (row.sms_help_keywords or "").strip().replace(" ", "")
        wk = (row.whatsapp_help_keywords or "").strip().replace(" ", "")
        if sk in old_kw_set:
            row.sms_help_keywords = NEW_KW
        if wk in old_kw_set:
            row.whatsapp_help_keywords = NEW_KW
        dm = (row.default_help_menu_text or "").strip()
        if dm.startswith(OLD_MENU_PREFIX) or OLD_BULLET in (row.default_help_menu_text or ""):
            row.default_help_menu_text = DEFAULT_HELP_MENU_TEXT
        old_uf = "Thanks - a helpdesk operator will respond soon."
        if (row.unknown_fallback_text or "").strip() == old_uf:
            row.unknown_fallback_text = (
                "Thanks — a helpdesk operator will respond soon. "
                "Send INFO, MENU, or COMMANDS for all keywords; APPOINT, ESCALATE, or RPM for requests."
            )


def _migrate_help_menu_symptom_checker_edition() -> None:
    """Refresh default help menu copy when it is still an older template without CHECK/SYMPTOMS explainer."""
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            return
        dm = row.default_help_menu_text or ""
        if (
            "CHECK or SYMPTOMS" in dm
            and "What each keyword is for" in dm
            and "RESET / START OVER" in dm
            and "You can also reply with a language name" in dm
        ):
            return
        if (
            "Next step: pick one line above" in dm
            or "Main menu (send any of these alone):" in dm
            or (dm.startswith("Kareconnect — send one line at a time.") and "RESET / START OVER" not in dm)
            or (dm.startswith("Kareconnect — send one line at a time.") and "You can also reply with a language name" not in dm)
        ):
            row.default_help_menu_text = DEFAULT_HELP_MENU_TEXT


def _migrate_kareconnect_branding() -> None:
    """Refresh stored copy that still says HealthConnect."""
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            return
        dm = row.default_help_menu_text or ""
        if dm.startswith("HealthConnect —") or dm.startswith("HealthConnect -"):
            row.default_help_menu_text = DEFAULT_HELP_MENU_TEXT
        uw = row.ussd_welcome_message or ""
        if "Welcome to HealthConnect" in uw:
            row.ussd_welcome_message = uw.replace("Welcome to HealthConnect", "Welcome to Kareconnect")


def _migrate_helpdesk_prefix_edition() -> None:
    """Refresh help menu and fallback text when they still describe the old free-text helpdesk flow."""
    with session_scope() as s:
        row = s.get(AppSettings, 1)
        if not row:
            return
        dm = row.default_help_menu_text or ""
        if "Start your message with Helpdesk" not in dm and (
            "optional AI" in dm
            or "always open a helpdesk ticket" in dm
            or "Send normal words, not a keyword" in dm
        ):
            row.default_help_menu_text = DEFAULT_HELP_MENU_TEXT
        uf = (row.unknown_fallback_text or "").strip()
        if uf.startswith("Thanks — a helpdesk operator"):
            row.unknown_fallback_text = (
                "We could not match that. Describe your symptoms in a short message, "
                "or send Helpdesk plus your question to reach our team. "
                "Send INFO, MENU, or COMMANDS for all keywords."
            )


def _ensure_users_recurring_opt_in_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS recurring_messages_opt_in BOOLEAN "
                "NOT NULL DEFAULT FALSE"
            )
        )


def _ensure_translation_cache_ml_columns() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE translation_cache ADD COLUMN IF NOT EXISTS provider VARCHAR(16) "
                "NOT NULL DEFAULT 'google'"
            )
        )
        conn.execute(text("ALTER TABLE translation_cache ADD COLUMN IF NOT EXISTS ml_model_version VARCHAR(64)"))


def _ensure_outbox_broadcast_run_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE messages_outbox ADD COLUMN IF NOT EXISTS broadcast_run_id VARCHAR(64)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_outbox_broadcast_run "
                "ON messages_outbox (broadcast_run_id)"
            )
        )


def _backfill_orphan_broadcast_outbox_links() -> None:
    """Link legacy broadcast outbox rows that were sent without broadcast_run_id."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE messages_outbox AS m
                SET broadcast_run_id = r.id
                FROM broadcast_runs AS r
                WHERE m.broadcast_run_id IS NULL
                  AND m.created_at = r.created_at
                  AND (
                    m.program = 'broadcast'
                    OR m.program = r.program
                  )
                """
            )
        )


def _ensure_users_helpdesk_hint_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_helpdesk_hint_date VARCHAR(10)"
            )
        )


def _ensure_whatsapp_voice_columns() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS whatsapp_voice_inbound_enabled "
                "BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS whatsapp_voice_outbound_enabled "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS whatsapp_voice_outbound_max_chars "
                "INTEGER NOT NULL DEFAULT 400"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_voice_replies_opt_in "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )


def _ensure_integration_paystack_calling_columns() -> None:
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS paystack_secret_key TEXT")
        )
        conn.execute(
            text("ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS paystack_public_key TEXT")
        )
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS meta_wa_calling_enabled "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS "
                "meta_wa_calling_webrtc_bridge_url TEXT"
            )
        )


def _ensure_clinical_action_tables() -> None:
    """New clinical models use create_all; columns below are additive on existing tables."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE integration_settings ADD COLUMN IF NOT EXISTS "
                "on_call_clinician_phone_e164 VARCHAR(32)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS priority VARCHAR(16) "
                "NOT NULL DEFAULT 'routine'"
            )
        )


def _ensure_telehealth_session_extra_columns() -> None:
    log = logging.getLogger(__name__)
    statements = [
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS payment_url TEXT",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS call_id VARCHAR(128)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS call_status VARCHAR(32)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS call_note TEXT",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS payment_account_number VARCHAR(32)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS payment_bank_name VARCHAR(128)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS payment_account_name VARCHAR(255)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS paystack_customer_email VARCHAR(255)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS call_started_at VARCHAR(32)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS call_ended_at VARCHAR(32)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS provider_joined BOOLEAN DEFAULT FALSE",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS feedback_prompt_sent_at VARCHAR(32)",
        "ALTER TABLE telehealth_sessions ADD COLUMN IF NOT EXISTS payment_instructions_sent_at VARCHAR(32)",
    ]
    with engine.begin() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
            except Exception as exc:
                log.warning("telehealth_sessions migration: %s — %s", sql[:72], exc)


def _ensure_app_settings_triage_keyword_column() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS triage_keyword VARCHAR(32) DEFAULT 'TRIAGE'"
            )
        )
        conn.execute(
            text(
                "UPDATE app_settings SET triage_keyword = 'TRIAGE' "
                "WHERE id = 1 AND (triage_keyword IS NULL OR TRIM(triage_keyword) = '')"
            )
        )


def _ensure_app_settings_telehealth_columns() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS telehealth_keyword VARCHAR(32) "
                "DEFAULT 'TELEHEALTH'"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS rate_keyword VARCHAR(32) DEFAULT 'RATE'"
            )
        )
        conn.execute(
            text(
                "UPDATE app_settings SET telehealth_keyword = 'TELEHEALTH' "
                "WHERE id = 1 AND (telehealth_keyword IS NULL OR TRIM(telehealth_keyword) = '')"
            )
        )
        conn.execute(
            text(
                "UPDATE app_settings SET rate_keyword = 'RATE' "
                "WHERE id = 1 AND (rate_keyword IS NULL OR TRIM(rate_keyword) = '')"
            )
        )


def _migrate_broadcast_template_language() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE integration_settings SET meta_wa_broadcast_template_language = 'en_US' "
                "WHERE meta_wa_broadcast_template_language IS NULL "
                "OR meta_wa_broadcast_template_language IN ('en', 'english', 'English')"
            )
        )


def _seed_telehealth_providers_if_empty() -> None:
    """Starter clinicians so TELEHEALTH works on fresh deploys (edit in Admin → Providers)."""
    from app.models import TelehealthProvider
    from app.util import new_id, now_iso

    now = now_iso()
    with session_scope() as s:
        from sqlalchemy import func, select

        total = s.execute(select(func.count()).select_from(TelehealthProvider)).scalar_one()
        if total:
            return
        starters = [
            ("Dr. Ada Okafor", "Dr.", "general_practice", 5000),
            ("Dr. Fatima Yusuf", "Dr.", "gynaecologist", 8000),
            ("Dr. Chidi Nwosu", "Dr.", "cardiology", 10000),
        ]
        for name, title, profession, fee in starters:
            s.add(
                TelehealthProvider(
                    id=new_id("thp"),
                    full_name=name,
                    title=title,
                    profession=profession,
                    bio="Sample listing — update name, fee, and WhatsApp in Admin → Telehealth → Providers.",
                    fee_ngn=fee,
                    currency="NGN",
                    status="active",
                    mask_patient_number=True,
                    created_at=now,
                    updated_at=now,
                )
            )


def _promote_draft_telehealth_providers_if_none_active() -> None:
    """Providers saved as Draft are hidden from patients; promote when none are Active yet."""
    with engine.begin() as conn:
        active = conn.execute(
            text("SELECT COUNT(*) FROM telehealth_providers WHERE status = 'active'")
        ).scalar_one()
        if int(active or 0) > 0:
            return
        draft = conn.execute(
            text("SELECT COUNT(*) FROM telehealth_providers WHERE status = 'draft'")
        ).scalar_one()
        if int(draft or 0) < 1:
            return
        now = now_iso()
        conn.execute(
            text(
                "UPDATE telehealth_providers SET status = 'active', updated_at = :now "
                "WHERE status = 'draft'"
            ),
            {"now": now},
        )


def _ensure_appointments_module_columns() -> None:
    cols = [
        ("channel", "VARCHAR(16)"),
        ("visit_type", "VARCHAR(16) DEFAULT 'in_person'"),
        ("scheduled_at", "VARCHAR(32)"),
        ("profession", "VARCHAR(64)"),
        ("provider_id", "VARCHAR(64)"),
        ("telehealth_session_id", "VARCHAR(64)"),
        ("ehr_appointment_id", "VARCHAR(64)"),
        ("ehr_sync_status", "VARCHAR(16) DEFAULT 'pending'"),
    ]
    with engine.begin() as conn:
        for name, typedef in cols:
            conn.execute(
                text(f"ALTER TABLE appointments ADD COLUMN IF NOT EXISTS {name} {typedef}")
            )
        conn.execute(
            text(
                "UPDATE appointments SET visit_type = 'in_person' "
                "WHERE visit_type IS NULL OR TRIM(visit_type) = ''"
            )
        )
        conn.execute(
            text(
                "UPDATE appointments SET ehr_sync_status = 'pending' "
                "WHERE ehr_sync_status IS NULL OR TRIM(ehr_sync_status) = ''"
            )
        )


def _ensure_triage_intake_columns() -> None:
    cols = [
        ("intake_stage", "VARCHAR(32) DEFAULT 'ask_age_group'"),
        ("intake_profile_json", "TEXT"),
        ("intake_completed_at", "VARCHAR(32)"),
    ]
    with engine.begin() as conn:
        for name, typedef in cols:
            conn.execute(
                text(f"ALTER TABLE triage_sessions ADD COLUMN IF NOT EXISTS {name} {typedef}")
            )
        conn.execute(
            text(
                "UPDATE triage_sessions SET intake_stage = 'ask_age_group' "
                "WHERE intake_stage IS NULL OR TRIM(intake_stage) = ''"
            )
        )
        conn.execute(
            text(
                "UPDATE triage_sessions SET intake_stage = 'complete', intake_completed_at = COALESCE(intake_completed_at, updated_at, created_at) "
                "WHERE chief_complaint IS NOT NULL AND TRIM(chief_complaint) <> ''"
            )
        )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_users_full_name_column()
    _ensure_integration_settings_columns()
    _ensure_helpdesk_ticket_ai_column()
    _ensure_app_settings_symptom_check_column()
    _ensure_users_recurring_opt_in_column()
    _ensure_translation_cache_ml_columns()
    _ensure_whatsapp_voice_columns()
    _ensure_users_helpdesk_hint_column()
    _ensure_outbox_broadcast_run_column()
    _ensure_app_settings_telehealth_columns()
    _ensure_app_settings_triage_keyword_column()
    _ensure_integration_paystack_calling_columns()
    _ensure_telehealth_session_extra_columns()
    _ensure_appointments_module_columns()
    _ensure_triage_intake_columns()
    _ensure_clinical_action_tables()
    _backfill_orphan_broadcast_outbox_links()
    _migrate_broadcast_template_language()
    _migrate_app_settings_twilio_reserved_help()
    _migrate_help_menu_symptom_checker_edition()
    _migrate_helpdesk_prefix_edition()
    _migrate_kareconnect_branding()
    _promote_draft_telehealth_providers_if_none_active()
    _seed_telehealth_providers_if_empty()

