"""Glucose reading OCR extraction, storage, 30-day trend analysis, and care recommendations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Optional

from sqlalchemy import select

from app.database import session_scope
from app.ehr_client import fetch_glucose_history, fetch_medication_regimen
from app.models import GlucoseReading
from app.util import new_id, now_iso

_GLUCOSE_PATTERNS = re.compile(
    r"(?:glucose|sugar|blood sugar|bg|mmol|mg/dl|mg/dL|mmol/L|reading)[\s:=-]*(\d{1,3}(?:\.\d{1,2})?)",
    re.I,
)
_STANDALONE_MGDL = re.compile(r"\b(\d{2,3})\s*(?:mg/dl|mg/dL|mg)\b", re.I)
_STANDALONE_MMOL = re.compile(r"\b(\d{1,2}(?:\.\d{1,2})?)\s*(?:mmol/L|mmol)\b", re.I)
_POST_MEAL = re.compile(r"\b(after meal|post meal|post-meal|after eating|2\s*h(?:our)?s? after)\b", re.I)
_FASTING = re.compile(r"\b(fasting|before meal|morning|empty stomach)\b", re.I)


@dataclass
class ExtractedGlucose:
    value_mg_dl: float
    reading_type: str  # fasting|post_meal|random|unknown
    raw_text: str


@dataclass
class GlucoseTrendAnalysis:
    readings_count: int = 0
    avg_mg_dl: float = 0.0
    post_meal_avg: float = 0.0
    fasting_avg: float = 0.0
    trend: str = "insufficient_data"  # stable|rising|consistently_elevated_post_meal|hypoglycemia_risk
    summary: str = ""
    plot_points: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GlucoseCarePlan:
    trend: GlucoseTrendAnalysis
    medication_regimen: list[str]
    draft_recommendations: list[str]
    dietary_guidance: str
    telehealth_session_id: Optional[str] = None
    draft_id: Optional[str] = None
    reminder_id: Optional[str] = None


def is_glucose_related(text: Optional[str]) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(r"\b(glucose|sugar|blood sugar|bg reading|sugar reading|mmol|mg/dl|diabetes meter)\b", t)
    )


def extract_glucose_from_text(text: str) -> Optional[ExtractedGlucose]:
    blob = (text or "").strip()
    if not blob:
        return None
    value: Optional[float] = None
    for m in _GLUCOSE_PATTERNS.finditer(blob):
        value = _parse_numeric(m.group(1), blob)
        if value:
            break
    if value is None:
        m = _STANDALONE_MGDL.search(blob)
        if m:
            value = float(m.group(1))
    if value is None:
        m = _STANDALONE_MMOL.search(blob)
        if m:
            value = round(float(m.group(1)) * 18.0182, 1)
    if value is None:
        nums = re.findall(r"\b(\d{2,3})\b", blob)
        if is_glucose_related(blob) and nums:
            value = float(nums[0])
    if value is None or value < 20 or value > 600:
        return None
    reading_type = "random"
    if _POST_MEAL.search(blob):
        reading_type = "post_meal"
    elif _FASTING.search(blob):
        reading_type = "fasting"
    return ExtractedGlucose(value_mg_dl=value, reading_type=reading_type, raw_text=blob[:500])


def _parse_numeric(token: str, blob: str) -> Optional[float]:
    try:
        v = float(token)
    except ValueError:
        return None
    if "mmol" in blob.lower() and v < 35:
        return round(v * 18.0182, 1)
    return v


def store_glucose_reading(
    *,
    phone_e164: str,
    user_id: Optional[str],
    value_mg_dl: float,
    reading_type: str,
    source: str,
    attachment_id: Optional[str] = None,
    ocr_excerpt: Optional[str] = None,
) -> str:
    rid = new_id("glu")
    now = now_iso()
    with session_scope() as s:
        s.add(
            GlucoseReading(
                id=rid,
                user_id=user_id,
                phone_e164=phone_e164,
                value_mg_dl=value_mg_dl,
                reading_type=reading_type,
                source=source,
                attachment_id=attachment_id,
                ocr_excerpt=(ocr_excerpt or "")[:500] or None,
                recorded_at=now,
                created_at=now,
            )
        )
    return rid


def analyze_30_day_trend(*, phone_e164: str, user_id: Optional[str]) -> GlucoseTrendAnalysis:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    points: list[tuple[str, float, str]] = []
    with session_scope() as s:
        local = list(
            s.execute(
                select(GlucoseReading)
                .where(GlucoseReading.phone_e164 == phone_e164, GlucoseReading.recorded_at >= cutoff)
                .order_by(GlucoseReading.recorded_at.asc())
            )
            .scalars()
            .all()
        )
        for r in local:
            points.append((r.recorded_at, float(r.value_mg_dl), r.reading_type))

    ehr_rows = fetch_glucose_history(phone_e164=phone_e164, days=30)
    for row in ehr_rows:
        points.append((row.get("recorded_at", ""), float(row.get("value_mg_dl", 0)), row.get("reading_type", "unknown")))

    points = [(t, v, rt) for t, v, rt in points if v > 0]
    points.sort(key=lambda x: x[0])

    if not points:
        return GlucoseTrendAnalysis(summary="No glucose readings in the last 30 days yet.")

    all_vals = [p[1] for p in points]
    post_vals = [p[1] for p in points if p[2] == "post_meal"]
    fast_vals = [p[1] for p in points if p[2] == "fasting"]
    avg_all = mean(all_vals)
    post_avg = mean(post_vals) if post_vals else 0.0
    fast_avg = mean(fast_vals) if fast_vals else 0.0

    trend = "stable"
    if post_vals and post_avg >= 180:
        trend = "consistently_elevated_post_meal"
    elif avg_all >= 200:
        trend = "rising"
    elif avg_all < 70:
        trend = "hypoglycemia_risk"
    elif len(all_vals) >= 4:
        first_half = mean(all_vals[: len(all_vals) // 2])
        second_half = mean(all_vals[len(all_vals) // 2 :])
        if second_half > first_half + 15:
            trend = "rising"

    summary_parts = [
        f"{len(points)} reading(s) over 30 days.",
        f"Average {avg_all:.0f} mg/dL.",
    ]
    if post_vals:
        summary_parts.append(f"Post-meal average {post_avg:.0f} mg/dL.")
    if trend == "consistently_elevated_post_meal":
        summary_parts.append("Pattern: consistently elevated after meals.")
    elif trend == "rising":
        summary_parts.append("Pattern: overall levels trending up.")
    elif trend == "hypoglycemia_risk":
        summary_parts.append("Pattern: possible low readings — review with clinician.")

    return GlucoseTrendAnalysis(
        readings_count=len(points),
        avg_mg_dl=round(avg_all, 1),
        post_meal_avg=round(post_avg, 1),
        fasting_avg=round(fast_avg, 1),
        trend=trend,
        summary=" ".join(summary_parts),
        plot_points=[
            {"recorded_at": t, "value_mg_dl": v, "reading_type": rt} for t, v, rt in points[-30:]
        ],
    )


def draft_medication_recommendations(
    *,
    trend: GlucoseTrendAnalysis,
    medications: list[str],
) -> list[str]:
    meds = medications or []
    recs: list[str] = []
    if trend.trend == "consistently_elevated_post_meal":
        recs.append(
            "Consider reviewing post-meal glucose targets; metformin dose timing or addition of "
            "short-acting therapy may be appropriate — clinician approval required."
        )
        if any("metformin" in m.lower() for m in meds):
            recs.append("Current metformin regimen: verify dose with largest meal; no auto-adjustment.")
        else:
            recs.append("No metformin on record — evaluate initiation per local diabetes protocol.")
    elif trend.trend == "rising":
        recs.append("Upward trend over 30 days — consider HbA1c review and therapy intensification.")
    elif trend.trend == "hypoglycemia_risk":
        recs.append("Low readings detected — review sulfonylurea/insulin doses; hypoglycemia counselling.")
    else:
        recs.append("Readings within routine follow-up range — continue current plan unless symptoms change.")
    recs.append("DRAFT ONLY — requires named clinician approval before any prescription change.")
    return recs


def dietary_guidance_for_trend(trend: str) -> str:
    if trend == "consistently_elevated_post_meal":
        return (
            "Interim dietary guidance: smaller carbohydrate portions per meal, add vegetables and protein, "
            "avoid sugary drinks, walk 10–15 minutes after eating if safe, and log readings before and 2 hours after meals."
        )
    if trend == "rising":
        return (
            "Interim guidance: reduce refined carbs, prefer whole grains, monitor portion sizes, "
            "and send readings every 2–3 days until your teleconsult."
        )
    if trend == "hypoglycemia_risk":
        return (
            "Interim guidance: do not skip meals, keep a fast-acting snack available, "
            "and seek urgent care if confused, sweaty, or faint."
        )
    return (
        "Continue balanced meals, limit sugary drinks, and send your next glucose photo with "
        "'fasting' or 'after meal' in the caption."
    )
