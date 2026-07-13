from __future__ import annotations

from typing import Any

from app.database import session_scope
from app.models import TwilioDeliveryEvent
from app.util import new_id, now_iso, safe_json_dumps


def record_twilio_status_form(form: dict[str, Any]) -> str:
    """
    Persist Twilio status callback (application/x-www-form-urlencoded).
    https://www.twilio.com/docs/sms/api/message-resource#message-status-values
    """
    fd = {str(k): str(v) for k, v in form.items()}
    sid = str(fd.get("MessageSid") or fd.get("SmsSid") or "").strip()
    status = str(fd.get("MessageStatus") or fd.get("SmsStatus") or "unknown").strip()
    err_code = str(fd.get("ErrorCode") or "").strip() or None
    err_msg = str(fd.get("ErrorMessage") or "").strip() or None
    to_v = str(fd.get("To") or "").strip() or None
    from_v = str(fd.get("From") or "").strip() or None
    if not sid:
        sid = f"unknown-{new_id('sid')[:12]}"

    row_id = new_id("tde")
    now = now_iso()
    with session_scope() as s:
        s.add(
            TwilioDeliveryEvent(
                id=row_id,
                message_sid=sid,
                message_status=status,
                error_code=err_code,
                error_message=err_msg,
                to_value=to_v,
                from_value=from_v,
                raw_json=safe_json_dumps(fd),
                created_at=now,
            )
        )
    return row_id
