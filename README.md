# Kareconnect (MVP)

Multi-channel patient education + helpdesk prototype:

- WhatsApp + SMS inbound via Twilio and/or [Africa's Talking](https://developers.africastalking.com/) webhooks
- QR-friendly join link + simple registration form (EDD supported)
- Programs: Maternal/Child Health, Chronic Diseases, Youth Health, Dialysis/Kidney
- Scheduled education messages + appointment reminders (sample content included)
- Helpdesk: FAQ auto-suggestions; unanswered questions escalate to operators (stub)
- Stubs for Telehealth escalation, Appointment booking, and RPM enrollment

## Quickstart (PostgreSQL)

1) Bootstrap (creates venv, installs deps, creates `.env`):

```bash
./scripts/bootstrap.sh
```

2) Start local Postgres (Homebrew):

```bash
./scripts/start_db.sh
```

This will start `postgresql@16` via `brew services` (if installed), then create the `healthconnect` role + database.

3) Run the server:

```bash
./scripts/start_api.sh
```

Open `http://localhost:8000` to join (works well for QR codes).

### One-command start (bootstrap + db + api)

```bash
./scripts/start_all.sh
```

## Admin console

- Admin UI: `http://localhost:8000/admin`
- **Login is required** for all admin pages (unauthenticated users are redirected to `/admin/login`).
- **Admin credentials are stored in Postgres** (`admin_users` table), bcrypt-hashed — there is **no default password** in the repo. Whoever bootstraps the server creates the account (see below). Usernames and passwords are whatever you chose when running `create_admin`.
- Create the first admin (or rotate a password) with:

```bash
./scripts/create_admin.sh admin 'choose-a-strong-password'
./scripts/create_admin.sh admin 'new-password' --update
```

For **Railway**, if you used the CLI flow from this README (including `railway run` + `create_admin.sh`), the username is typically **`admin`** and the password is the string you passed to that script — change it immediately with `--update` if it was temporary.

- Set a strong `HC_ADMIN_SESSION_SECRET` in production (used to sign the admin session cookie). This value must stay in **environment variables** (not the admin UI); it boots the app before DB-backed settings apply.
- Open **Deployment & integrations** in admin to set production URLs and credentials (Meta WhatsApp Cloud API, Twilio, Africa's Talking, optional operator webhook). Any **non-empty** field there overrides the matching environment variable at runtime.
- Open **Channel Settings** in admin to configure:
  - USSD dial code and USSD menu texts
  - SMS and WhatsApp join/help keywords
  - command keywords (`LANG`, `STOP`, `START`, `APPOINT`, `ESCALATE`, `RPM`)
  - default auto-reply/help text behavior

## Deploy (Railway)

This API is a **long-running** FastAPI process with a background scheduler and PostgreSQL, which fits [Railway](https://railway.app/) well.

### This repo’s CLI setup (already done once)

If you used `railway init` / `railway deploy -t postgres` / `railway up` from this directory, you will have:

- **Project:** `HealthConnect` on Railway (app name **Kareconnect**; see [Railway dashboard](https://railway.com/dashboard)).
- **Services:** `Postgres` (database) and `healthconnect-api` (this app).
- **`.railwayignore`** — keeps `railway up` uploads small (excludes `.venv`, local SQLite files, etc.).
- **Git repo** — initialized so the CLI does not upload the whole folder as an unbounded archive.

The API service should have:

- `DATABASE_URL=${{Postgres.DATABASE_URL}}` (internal hostname; correct **inside** Railway).
- `HC_ADMIN_SESSION_SECRET` (long random).
- `HC_BASE_URL` set to your public API URL (see **Domains** for the `*.up.railway.app` hostname).
- Optional: `RAILWAY_DATABASE_PUBLIC_URL=${{Postgres.DATABASE_PUBLIC_URL}}` — used only so you can run **local** maintenance commands against the cloud DB (see below).

### Fresh manual setup (if you start over)

1. In Railway: **New Project** → **Provision PostgreSQL**, then **Deploy from GitHub** (this repo) or **Empty service** + connect the repo.
2. On the **web** service, set environment variables at minimum:
   - `DATABASE_URL` — use the **Postgres** plugin’s connection string (SQLAlchemy URL, e.g. `postgresql+psycopg2://…`).
   - `HC_ADMIN_SESSION_SECRET` — long random string.
3. After the first deploy, create an admin (see **Create admin from your laptop** below).
4. Open **`https://<your-railway-domain>/admin`** → **Deployment & integrations** to store Meta/Twilio/AT secrets if you prefer not to use env vars.
5. In **Meta for Developers** → App settings, set **Privacy policy URL** to **`https://<your-railway-domain>/privacy`** (public page included in this repo). In **WhatsApp** → **Configuration**, set the webhook to **`https://<your-railway-domain>/webhooks/meta/whatsapp`** and the same **verify token** you saved in admin. Under **Webhook fields**, turn **messages** **on** — if all fields are off, Meta will not deliver inbound chats and the app cannot reply. Set **`META_GRAPH_VERSION`** (or admin **meta_graph_version**) to match your app (for example **`v25.0`**).

The repo includes **`railway.toml`** so each deploy runs **`releaseCommand`**: `python -c "from app.db import init_db; init_db()"` to apply DB schema/data patches (`ALTER TABLE …`, settings migrations), then starts **`uvicorn app.main:app --host 0.0.0.0 --port $PORT`**. The app also calls **`init_db()`** on startup so local runs and new replicas stay aligned.

### Create admin from your laptop

`railway run` executes **on your machine** with Railway env vars. The default `DATABASE_URL` points at `postgres.railway.internal`, which **does not resolve locally**. If you added `RAILWAY_DATABASE_PUBLIC_URL` (see above), run:

```bash
railway service link healthconnect-api
railway run --service healthconnect-api bash -c \
  'export DATABASE_URL="$RAILWAY_DATABASE_PUBLIC_URL" && ./scripts/create_admin.sh admin '"'"'your-strong-password'"'"''
```

Then sign in at `https://<your-railway-host>/admin` and **change the password** if you used a temporary one.

### Vercel landing (optional)

The **full API** should stay on **Railway** (persistent process + Postgres + scheduler). For a lightweight public page only, use the **`vercel-landing/`** folder: `cd vercel-landing && vercel link --yes --project <name> && vercel deploy --prod --yes`. Example production URL: `https://healthconnect-landing.vercel.app`.

## WhatsApp / SMS (Twilio-compatible)

### Patient “Open WhatsApp” link (join page)

Set **`HC_WHATSAPP_NUMBER`** (env) or **Deployment & integrations → WhatsApp number (digits only)** in admin to your WhatsApp Business number **digits only** (no `+`), for example `2348012345678`. The join page then shows a **Open WhatsApp** button with the **first WhatsApp join keyword** from Admin → Channel Settings (comma-separated list).

### QR code for the join page

Set **`HC_BASE_URL`** (env) or **Deployment & integrations → Public base URL** in admin to your public site root (for example `https://healthconnect.example.com`). The join page shows a QR image at **`GET /join/qr.png`**, which encodes **`{HC_BASE_URL}/`** so scans open the same join form.

### Meta WhatsApp Cloud API

- Webhook: **`GET` + `POST`** `{HC_BASE_URL}/webhooks/meta/whatsapp` (verify token + inbound JSON).
- When **Phone number ID** and **Access token** are set (admin or env), **outbound WhatsApp** uses Meta; otherwise Twilio is used if Twilio is configured.
- **Inbound replies require** the WhatsApp product webhook to subscribe to the **`messages`** field in the Meta developer console. **`META_GRAPH_VERSION`** (default in code **`v25.0`**) should match the API version you use in Meta.

### Inbound / outbound (Twilio)

Set the Twilio webhook to:

- `POST /webhooks/twilio/inbound` (when a user sends SMS/WhatsApp to your number)
- `POST /webhooks/twilio/status` (**delivery receipts** — set as **Status callback URL** on the Messaging Service under **Integration**, or on the phone number; Kareconnect also sets `StatusCallback` on outbound sends when **`HC_BASE_URL`** is `https://…`)

In **Admin → Webhooks reference** you can copy full URLs and run safe self-tests.

Configure **`TWILIO_ACCOUNT_SID`**, **`TWILIO_AUTH_TOKEN`**, and **`TWILIO_MESSAGING_SERVICE_SID`** in env or admin (Messaging Service should include your SMS and/or WhatsApp senders). Outbound uses the same API; WhatsApp recipients are addressed as `whatsapp:+E164`.

**SMS “JOIN” number:** Twilio does not infer this automatically. Use the **Twilio phone number** (or long code) that **receives** patient SMS and is wired in the Twilio console to **`POST {HC_BASE_URL}/webhooks/twilio/inbound`**. Put that number in **`HC_SMS_JOIN_NUMBER`** (env) or **Deployment & integrations → SMS number patients text JOIN to** so the join page can show it and build an `sms:` link. You will find it under **Phone Numbers → Manage → Active numbers** (or as a sender on your **Messaging Service**).

If Twilio is not configured, outbound sends are skipped (outbox still marks messages sent for local testing).

### Africa's Talking (SMS + USSD)

See [Africa's Talking documentation](https://developers.africastalking.com/) for dashboard setup.

- **Inbound SMS webhook:** `POST /webhooks/africastalking/sms` (form fields per [SMS notifications](https://developers.africastalking.com/docs/sms/notifications)). The handler returns plain **`OK`**; user-facing replies are sent with the [SMS sending API](https://developers.africastalking.com/docs/sms/sending) when **`AT_USERNAME`**, **`AT_API_KEY`**, and **`AT_SHORTCODE`** are set (env or admin).
- **USSD callback:** `POST /webhooks/africastalking/ussd` — response is plain text **`CON\n...`** or **`END\n...`** per [USSD](https://developers.africastalking.com/docs/ussd/overview).

When AT credentials are set, **outbound SMS** from the outbox uses Africa's Talking; otherwise SMS uses Twilio if Twilio is configured.

## Patient commands (SMS / WhatsApp) & helpdesk

### What the app replies to (preset text)

Handled in **`app/inbound_chat.py`** (keywords come from **Admin → Channel settings**, except fixed `LANG` / `APPOINT` / `ESCALATE` / `RPM` prefixes):

| User text (examples) | Reply |
|----------------------|--------|
| **JOIN** (+ optional programmes / EDD) | Enrolment welcome + queues programme messages |
| **INFO**, **MENU**, **COMMANDS** (comma lists per channel in Channel settings) | `default_help_menu_text` from Channel settings |
| **LANG** + **EN** / **PCM** / **HA** / **IG** / **YO** (English / Pidgin / Hausa / Igbo / Yoruba) | Language updated |
| **STOP** / **START** | Pause / resume programme messages |
| **APPOINT …** | Appointment stub + reference id |
| **ESCALATE …** | Escalation stub + urgency |
| **RPM …** | RPM request stub |
| **Helpdesk** … (prefix) | Opens a **helpdesk ticket**; FAQ suggestion when matched; staff can reply on the same chat |
| **Anything else** | FAQ + symptom checker (KareVault when enabled); does **not** open a helpdesk ticket; if no match and **auto_reply_enabled**, sends `unknown_fallback_text` |

FAQ pairs live in **`app/content/faqs.json`** — add more Q/A rows to improve “intelligence” without code changes.

### Twilio SMS: HELP is often reserved

Twilio **Advanced Opt-Out** / **A2P / toll-free compliance** can answer **HELP** **before** your webhook, so patients only see a short carrier line. **JOIN** usually still reaches the app.

**Default behaviour:** Kareconnect uses **INFO**, **MENU**, and **COMMANDS** (not HELP) in **SMS help keywords** and **`default_help_menu_text`**. Existing databases are migrated on startup if they still had the old `HELP,INFO,MENU` defaults. You can still add other words in **Admin → Channel settings**.

### Helpdesk: activation, routing, operator replies

- **Activation:** Messages that **start with `Helpdesk`** (case-insensitive) open a **helpdesk ticket** (`Helpdesk` in admin) and run FAQ suggestion. Other free-text messages use the symptom checker only.
- **Who sees it:** Staff with admin access open **Helpdesk** (list + ticket detail). There is **no built-in email/Slack** unless you wire **`HC_OPERATOR_WEBHOOK_URL`** / **Deployment & integrations → operator webhook URL**.
- **Operator webhook:** On each new ticket, the app **POSTs JSON** to that URL: `event`, `ticket_id`, `admin_url` (deep link to the ticket in this admin UI), `from_phone_e164`, `channel`, `question`, `suggested_answer`. Your CRM/helpdesk can ingest that and notify humans.
- **Routing answers back to the patient:** Not automated yet. Today: staff read the ticket in admin and **manually** send an SMS/WhatsApp from your clinic workflow, **or** your external system calls Twilio/Meta APIs using the phone on the ticket. A future improvement would be an authenticated **`POST /api/operator/ticket-reply`** (or similar) that queues `send_message` to the ticket’s `from_phone_e164` on the same `channel`.

### KareVault symptom checker (optional AI after FAQ)

When **`HC_KAREVAULT_ENABLED=1`**, each non-command patient message that is **not** prefixed with **Helpdesk** runs the FAQ matcher first, then calls the [KareBot Symptom Checker API](https://symptoms-checker.karevault.com/api/docs/) (`/api/external/v1/chat/` with partner access, or `/api/ai/chat/` if no partner email is set). Active programme enrolments (**maternal_child**, **chronic**, **youth**, **kidney**) are sent as context so replies stay on-topic. Helpdesk-prefixed messages open a ticket only (no KareVault call). Symptom-checker output is **informational only**, not a diagnosis.

Configure in **`.env`**: `HC_KAREVAULT_BASE_URL`, **`HC_KAREVAULT_PARTNER_EMAIL`** + **`HC_KAREVAULT_PARTNER_NAME`** (recommended), or enable without email to use the public chat endpoint (suitable for demos only).

### Making replies “smarter”

1. Expand **`app/content/faqs.json`** (more questions, clearer wording).  
2. Tune **Channel settings** help/stop/start keyword lists and **`default_help_menu_text`**.  
3. Optionally lower/raise FAQ similarity in code (`best_faq_answer` threshold in **`app/util.py`**).  
4. Tune **KareVault** env vars above, or integrate a dedicated CX platform via **`operator_webhook_url`**.

## USSD

USSD onboarding (e.g. `*134*550*2#`) varies by aggregator. This app exposes:

- **`POST /webhooks/africastalking/ussd`** — Africa's Talking–style plain text `CON` / `END` responses.
- **`POST /api/ussd/session`** — JSON `{ "session_id", "phone_e164", "text" }` for gateways that prefer JSON.

## Core APIs (stubs but functional storage)

- `POST /api/escalations` (telehealth escalation request)
- `POST /api/appointments` (appointment booking request)
- `POST /api/rpm/enroll` (RPM enrollment request)

## Notes

- Database is PostgreSQL (tables created automatically on first run).
- Content lives under `app/content/`.
