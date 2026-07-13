"""
Language identification with Facebook FastText lid.176 (~176 languages).

Model file: ml_cache/lid.176.ftz (download via scripts/download_ml_assets.py).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional, Tuple

_log = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LID_PATH = ROOT_DIR / "ml_cache" / "lid.176.ftz"


def lid_model_path() -> Path:
    override = (os.getenv("FASTTEXT_LID_PATH") or "").strip()
    return Path(override) if override else DEFAULT_LID_PATH


def is_fasttext_available() -> bool:
    p = lid_model_path()
    return p.is_file() and p.stat().st_size > 100_000


def _load_fasttext():
    global _model
    if _model is not None:
        return _model
    import fasttext  # noqa: PLC0415 — heavy import after download

    path = lid_model_path()
    if not path.is_file():
        raise FileNotFoundError(f"FastText lid model missing: {path}")
    _model = fasttext.load_model(str(path))
    return _model


# FastText ISO codes → Kareconnect / Marian codes (OPUS-MT uses two-letter where applicable).
_FASTTEXT_TO_APP = {
    "ibo": "ig",
}


def _normalize_lang(code: str) -> str:
    c = (code or "").lower().strip().replace("__label__", "")
    return _FASTTEXT_TO_APP.get(c, c)


def normalize_app_language_code(code: str) -> str:
    """Map FastText / ISO variants to Kareconnect language codes (ig, ha, yo, en, …)."""
    return _normalize_lang(code)


def detect_language_neural(text: str) -> Optional[Tuple[str, float, str]]:
    """
    Returns (language_code, confidence, model_label) or None if unavailable / empty input.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if not is_fasttext_available():
        _log.warning("FastText lid model not found at %s", lid_model_path())
        return None
    line = re.sub(r"\s+", " ", raw)[:5000]
    try:
        with _lock:
            m = _load_fasttext()
            labels, probs = m.predict(line, k=1)
        lab = str(labels[0]).replace("__label__", "")
        conf = float(probs[0])
        lang = _normalize_lang(lab)
        return lang, conf, "fasttext-lid176"
    except Exception as exc:
        _log.warning("FastText predict failed: %s", exc)
        return None
