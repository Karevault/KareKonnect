#!/usr/bin/env python3
"""Smoke-test Pidgin Marian translation (en → pcm). Run from repo root after pip install -r requirements.txt."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = "Welcome to Kareconnect. Drink plenty of water and rest if you feel unwell."


def main() -> int:
    print("Testing en → pcm (Marian)…")
    from app.neural_marian import translate_neural
    from app.translation import translate_outbound_for_user

    neural = translate_neural(text=SAMPLE, source_lang="en", target_lang="pcm")
    if not neural:
        print("FAIL: Marian returned None (check torch>=2.6 and model download).")
        return 1
    pidgin, conf, model_id = neural
    print(f"Model: {model_id} (confidence {conf})")
    print(f"EN:  {SAMPLE}")
    print(f"PCM: {pidgin}")
    if pidgin.strip() == SAMPLE.strip():
        print("WARN: output equals input — translation may not have run.")
        return 1

    via_pipeline = translate_outbound_for_user(SAMPLE, "pcm")
    print(f"\nVia translate_outbound_for_user (LANG pcm):\n{via_pipeline}")
    if via_pipeline.strip() == SAMPLE.strip():
        print("FAIL: outbound pipeline still returning English.")
        return 1
    print("\nOK: Pidgin translation is working.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
