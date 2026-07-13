"""
Neural machine translation (Marian) for Nigerian Pidgin (pcm) ↔ English only.

Hausa, Igbo, Yoruba use Google Cloud Translation (see app.translation).
Google does not support pcm; these NITHUB checkpoints are used instead.

Models load from Hugging Face Hub (ml_cache/hf_hub at deploy). Requires torch>=2.6 (pytorch_model.bin).
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

_log = logging.getLogger(__name__)

_lock = threading.Lock()

MARIAN_MODEL_IDS: dict[tuple[str, str], str] = {
    ("en", "pcm"): "NITHUB-AI/marian-mt-bbc-en-pcm",
    ("pcm", "en"): "NITHUB-AI/marian-mt-bbc-pcm-en",
}

_MAX_PIPELINES = max(1, int(os.getenv("HC_NEURAL_MAX_LOADED_MODELS", "2")))
# model_id -> (tokenizer, model)
_model_cache: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
_warmup_started = False


def _normalize_pair(src: str, tgt: str) -> tuple[str, str]:
    return src.strip().lower()[:8], tgt.strip().lower()[:8]


def _get_model_and_tokenizer(model_id: str) -> tuple[Any, Any]:
    from transformers import MarianMTModel, MarianTokenizer  # noqa: PLC0415

    global _model_cache
    with _lock:
        if model_id in _model_cache:
            _model_cache.move_to_end(model_id)
            return _model_cache[model_id]
        while len(_model_cache) >= _MAX_PIPELINES:
            old_key, _ = _model_cache.popitem(last=False)
            _log.info("Evicted Marian model cache entry %s", old_key)
        _log.info("Loading Marian MT model %s …", model_id)
        tokenizer = MarianTokenizer.from_pretrained(model_id)
        model = MarianMTModel.from_pretrained(model_id)
        model.eval()
        _model_cache[model_id] = (tokenizer, model)
        return tokenizer, model


def _generate(tokenizer: Any, model: Any, text: str, *, max_length: int = 256) -> str:
    import torch  # noqa: PLC0415

    inputs = tokenizer([text], return_tensors="pt", padding=True, truncation=True, max_length=512)
    with torch.no_grad():
        # num_beams=1 is much faster on CPU — important for SMS/WhatsApp webhook timeouts.
        outputs = model.generate(**inputs, max_length=max_length, num_beams=1)
    return tokenizer.decode(outputs[0], skip_special_tokens=True).strip()


def warmup_marian_models() -> None:
    """Load all Pidgin Marian checkpoints (call once at startup or from a background thread)."""
    for model_id in dict.fromkeys(MARIAN_MODEL_IDS.values()):
        _get_model_and_tokenizer(model_id)
    _log.info("Pidgin Marian warmup complete (%s models)", len(_model_cache))


def marian_loaded_model_ids() -> list[str]:
    """Model IDs currently held in the in-process LRU (empty until first use or warmup)."""
    with _lock:
        return list(_model_cache.keys())


def start_marian_warmup_background() -> None:
    """Non-blocking startup preload so the first patient message is not stuck loading weights."""
    global _warmup_started
    if _warmup_started:
        return
    _warmup_started = True

    def _run() -> None:
        try:
            warmup_marian_models()
        except Exception as exc:
            _log.warning("Pidgin Marian background warmup failed: %s", exc)

    threading.Thread(target=_run, name="marian-warmup", daemon=True).start()


def translate_neural(
    *, text: str, source_lang: str, target_lang: str
) -> Optional[Tuple[str, float, str]]:
    """
    Returns (translation, confidence, model_id) or None if not a pcm pair (caller uses Google).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    src, tgt = _normalize_pair(source_lang, target_lang)
    model_id = MARIAN_MODEL_IDS.get((src, tgt))
    if not model_id:
        return None
    try:
        tokenizer, model = _get_model_and_tokenizer(model_id)
        with _lock:
            translated = _generate(tokenizer, model, raw[:4000])
        if not translated:
            return None
        return translated, 0.88, model_id
    except Exception as exc:
        _log.warning("Marian translation failed (%s): %s", model_id, exc)
        return None
