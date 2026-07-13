#!/usr/bin/env python3
"""
Download neural translation assets for deploy / CI.

- FastText lid.176 → ml_cache/lid.176.ftz (language detection, all LANG codes)
- Marian Pidgin only → ml_cache/hf_hub (en↔pcm; ha/ig/yo use Google Translate at runtime)

Default HC_PREFETCH_MARIAN=pcm (only two NITHUB checkpoints).
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ML_CACHE = ROOT / "ml_cache"
HF_HOME_DIR = ML_CACHE / "hf_hub"
FASTTEXT_LID_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"
FASTTEXT_LID_FILE = ML_CACHE / "lid.176.ftz"


def _download_fasttext_lid() -> None:
    ML_CACHE.mkdir(parents=True, exist_ok=True)
    if FASTTEXT_LID_FILE.exists() and FASTTEXT_LID_FILE.stat().st_size > 100_000:
        print(f"OK: {FASTTEXT_LID_FILE} already present ({FASTTEXT_LID_FILE.stat().st_size} bytes)")
        return
    print(f"Downloading FastText lid.176.ftz → {FASTTEXT_LID_FILE} …")
    urllib.request.urlretrieve(FASTTEXT_LID_URL, FASTTEXT_LID_FILE)
    print(f"OK: wrote {FASTTEXT_LID_FILE.stat().st_size} bytes")


def _prefetch_marian_models() -> None:
    """Snapshot Pidgin Marian checkpoints into ml_cache/hf_hub (HF_HOME at runtime)."""
    HF_HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(HF_HOME_DIR)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(HF_HOME_DIR / "hub")

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        print(f"SKIP Marian prefetch (huggingface_hub): {exc}", file=sys.stderr)
        return

    sys.path.insert(0, str(ROOT))
    from app.neural_marian import MARIAN_MODEL_IDS

    mode = (os.getenv("HC_PREFETCH_MARIAN") or "pcm").strip().lower()
    if mode in ("0", "false", "no", "off", "skip"):
        print("SKIP Marian prefetch (HC_PREFETCH_MARIAN=off)")
        return

    model_ids = list(dict.fromkeys(MARIAN_MODEL_IDS.values()))
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    for model_id in model_ids:
        print(f"Prefetching Pidgin Marian weights: {model_id} …")
        try:
            snapshot_download(repo_id=model_id)
            print(f"OK: {model_id}")
            ok.append(model_id)
        except Exception as exc:
            msg = str(exc).strip() or type(exc).__name__
            print(f"WARN: could not prefetch {model_id}: {msg}", file=sys.stderr)
            failed.append((model_id, msg))

    print(f"Pidgin Marian prefetch: {len(ok)} ok, {len(failed)} failed")
    if failed:
        for mid, err in failed:
            print(f"  - {mid}: {err[:200]}", file=sys.stderr)


def main() -> int:
    _download_fasttext_lid()
    _prefetch_marian_models()
    print(f"ML assets ready under {ML_CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
