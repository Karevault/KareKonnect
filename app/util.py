from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional, List


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


DISPLAY_TZ = "Africa/Lagos"


def _parse_iso_datetime(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_datetime_human(value: str | datetime | None, *, tz_name: str = DISPLAY_TZ) -> str:
    """User-facing date and time in local timezone, e.g. 5 Jun 2026, 7:30 PM."""
    if value is None or value == "":
        return "—"
    dt = _parse_iso_datetime(value)
    if dt is None:
        return str(value).strip() or "—"
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt.astimezone(timezone.utc)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.strftime('%M %p')}"


def format_date_human(value: str | date | None) -> str:
    """User-facing calendar date, e.g. 5 Jun 2026."""
    if value is None or value == "":
        return "—"
    if isinstance(value, date) and not isinstance(value, datetime):
        d = value
    else:
        raw = str(value).strip()
        if len(raw) >= 10 and raw[4] == "-":
            try:
                d = date.fromisoformat(raw[:10])
            except ValueError:
                return raw
        else:
            return raw
    return f"{d.day} {d.strftime('%b %Y')}"


def install_jinja_datetime_filters(env) -> None:
    env.filters["format_dt"] = format_datetime_human
    env.filters["format_date"] = format_date_human


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def format_help_keywords_natural_list(raw: Optional[str], *, max_terms: int = 5) -> str:
    """Human-readable list for join page hints, e.g. INFO, MENU, or COMMANDS."""
    parts = [p.strip().upper() for p in (raw or "").replace(";", ",").split(",") if p.strip()][:max_terms]
    if not parts:
        return "INFO, MENU, or COMMANDS"
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} or {parts[1]}"
    return ", ".join(parts[:-1]) + f", or {parts[-1]}"


def normalize_inbound_body(value: str) -> str:
    """Strip invisible chars / odd spaces so keywords (JOIN, INFO, …) match reliably over SMS."""
    t = (value or "").replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\xa0", " ")
    return " ".join(t.split()).strip()


def normalize_phone_e164(value: str) -> str:
    v = value.strip()
    if v.startswith("whatsapp:"):
        v = v[len("whatsapp:") :]
    v = re.sub(r"\s+", "", v)
    return v


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class FAQ:
    q: str
    a: str


def load_faqs() -> list[FAQ]:
    fp = Path(__file__).parent / "content" / "faqs.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    return [FAQ(q=x["q"], a=x["a"]) for x in data]


def best_faq_answer(question: str, faqs: List[FAQ], threshold: float = 0.52) -> Optional[str]:
    q = question.strip().lower()
    if not q:
        return None

    best = None
    best_score = 0.0
    for f in faqs:
        score = SequenceMatcher(a=q, b=f.q.strip().lower()).ratio()
        if score > best_score:
            best_score = score
            best = f

    if best and best_score >= threshold:
        return best.a
    return None

