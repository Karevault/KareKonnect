# WhatsApp Voice Integration (Kareconnect)

## Implemented (all phases)

### Phase 1 — Inbound voice notes (STT)

- Meta webhook handles `type: audio` messages.
- Media is downloaded via Graph API (`app/meta_whatsapp.py` → `download_media`).
- Speech-to-text via Google Cloud Speech-to-Text REST (`app/google_voice.py`).
- Transcript is passed into the existing text router (`handle_inbound_text_message`).
- On failure: patient receives a clear fallback asking them to type or retry.
- Toggle: **Admin → Channel Settings → WhatsApp: accept voice notes**, or `HC_WHATSAPP_VOICE_INBOUND`.

### Phase 2 — Outbound voice replies (TTS, opt-in)

- Patients opt in with **VOICE YES** / out with **VOICE NO** (WhatsApp only).
- Short replies (default ≤400 chars) can be sent as `type: audio` after Google Text-to-Speech (OGG Opus).
- Upload to Meta media API, then send audio message; **always falls back to text** if TTS or send fails.
- **Webhook replies are always text** (`allow_voice=False`) so Meta gets a fast response; synchronous TTS in the webhook was causing silent failures/timeouts.
- Toggle: **Admin → Channel Settings → WhatsApp: allow VOICE YES patients…**, or `HC_WHATSAPP_VOICE_OUTBOUND=1`.
- Scheduled programme outbox messages remain **text only** (interactive replies only).

### Phase 3 — Operations

- **Voice message logs** table + admin list (Translation & ML → Voice message logs).
- **GET `/healthz/voice`** — configuration probe (no live STT/TTS call).
- Help menu / JOIN copy documents voice notes and VOICE YES/NO.

## Configuration

| Variable | Purpose |
|----------|---------|
| `META_WA_*` | WhatsApp Cloud API (required for voice) |
| `GOOGLE_TRANSLATE_API_KEY` or `GOOGLE_CLOUD_API_KEY` | STT + TTS (enable **Speech-to-Text** and **Text-to-Speech** APIs on the GCP project) |
| `HC_WHATSAPP_VOICE_INBOUND` | Default `1` |
| `HC_WHATSAPP_VOICE_OUTBOUND` | Default `0` (opt-in outbound) |
| `HC_WHATSAPP_VOICE_MAX_CHARS` | Max reply length for TTS (default `400`) |

## Patient commands (WhatsApp)

- Send a **voice note** — transcribed and routed like typed text (when inbound voice is enabled).
- **VOICE YES** — opt in to short voice-note replies.
- **VOICE NO** — text-only replies.

## Code map

| Module | Role |
|--------|------|
| `app/meta_whatsapp.py` | Media download/upload, `send_audio_message`, `iter_inbound_audio_messages` |
| `app/google_voice.py` | `speech_to_text`, `text_to_speech_ogg` |
| `app/whatsapp_voice.py` | Orchestration, logging, `send_whatsapp_patient_reply` |
| `app/main.py` | Meta webhook audio branch |
| `app/inbound_chat.py` | VOICE YES/NO handling |

## Risks (unchanged)

- STT quality may be weaker for Pidgin / mixed language.
- Voice/audio is sensitive health data — review retention policy for `voice_message_logs`.
- Added latency and Google API cost per voice note.

## Public probes

- `GET /healthz/voice` — voice feature flags and provider readiness
- `GET /healthz/messaging` — channel providers
