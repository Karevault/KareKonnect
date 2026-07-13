"""WhatsApp customer-service window (24h) helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from app.database import session_scope
from app.models import InboundMessage


def whatsapp_session_open(phone_e164: str, *, hours: float = 24.0) -> bool:
    """True when the user sent WhatsApp inbound within the last `hours` hours."""
    phone = (phone_e164 or "").strip()
    if not phone:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with session_scope() as s:
        last_at = s.execute(
            select(InboundMessage.created_at)
            .where(
                InboundMessage.channel == "whatsapp",
                InboundMessage.from_phone_e164 == phone,
            )
            .order_by(desc(InboundMessage.created_at))
            .limit(1)
        ).scalar_one_or_none()
    if not last_at:
        return False
    try:
        ts = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= cutoff
    except ValueError:
        return False
