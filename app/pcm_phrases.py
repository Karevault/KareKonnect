"""Pre-written Pidgin lines for short system replies (no Marian latency on LANG / acks)."""

# Keyed by English template id; expand as needed.
PCM_PHRASES: dict[str, str] = {
    "language_updated": (
        "We don set your language to Pidgin (PCM). "
        "Send INFO, MENU, or COMMANDS for all commands."
    ),
    "messages_paused": "We don pause programme messages. Send START to continue.",
    "messages_resumed": "Programme messages don start again.",
}
