"""Staff broadcast: target enrolled members by programme and enqueue outbound messages."""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Literal, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.models import BroadcastRun, Enrollment, MessageOutbox, User
from app.services import send_queued_messages
from app.translation import translate_outbound_for_user
from app.util import new_id, now_iso

_log = logging.getLogger(__name__)
_BROADCAST_TRANSLATE_TIMEOUT_SEC = 8.0
_LANG_DELIVERY_ORDER = ("en", "pcm", "ha", "ig", "yo")
DeliverableChannel = Literal["sms", "whatsapp"]
VALID_PROGRAMS = frozenset({"maternal_child", "chronic", "youth", "kidney"})
_LANG_LABELS = {
    "en": "English",
    "pcm": "Pidgin (Naija)",
    "ha": "Hausa",
    "ig": "Igbo",
    "yo": "Yoruba",
}


@dataclass(frozen=True)
class BroadcastFilters:
    program: str  # "all" or a programme code
    enrollment_status: str = "active"
    channel: Optional[str] = None  # None = sms + whatsapp
    language: Optional[str] = None


def _normalize_filters(filters: BroadcastFilters) -> BroadcastFilters:
    program = (filters.program or "all").strip().lower()
    if program != "all" and program not in VALID_PROGRAMS:
        raise ValueError(f"Unknown programme: {filters.program}")
    status = (filters.enrollment_status or "active").strip().lower()
    channel = (filters.channel or "").strip().lower() or None
    if channel and channel not in ("sms", "whatsapp"):
        raise ValueError("Channel filter must be sms, whatsapp, or empty for both.")
    language = (filters.language or "").strip().lower() or None
    if language and language not in ("en", "pcm", "ha", "ig", "yo"):
        raise ValueError(f"Unsupported language filter: {filters.language}")
    return BroadcastFilters(program=program, enrollment_status=status, channel=channel, language=language)


def _audience_user_ids_stmt(filters: BroadcastFilters):
    """One row per user even when enrolled in multiple programmes."""
    clauses = [Enrollment.status == filters.enrollment_status]
    if filters.program != "all":
        clauses.append(Enrollment.program == filters.program)
    if filters.channel:
        clauses.append(User.channel == filters.channel)
    else:
        clauses.append(User.channel.in_(["sms", "whatsapp"]))
    if filters.language:
        clauses.append(User.language == filters.language)
    return (
        select(User.id)
        .join(Enrollment, Enrollment.user_id == User.id)
        .where(and_(*clauses))
        .distinct()
    )


def _load_audience_users(s, filters: BroadcastFilters) -> list[User]:
    user_ids = list(s.execute(_audience_user_ids_stmt(filters)).scalars().all())
    if not user_ids:
        return []
    return list(
        s.execute(select(User).where(User.id.in_(user_ids)).order_by(User.phone_e164.asc())).scalars().all()
    )


def count_broadcast_recipients(filters: BroadcastFilters) -> int:
    f = _normalize_filters(filters)
    with session_scope() as s:
        return len(s.execute(_audience_user_ids_stmt(f)).scalars().all())


def _language_label(code: str) -> str:
    return _LANG_LABELS.get((code or "en").lower().strip(), code)


def _language_delivery_order(languages: list[str]) -> list[str]:
    seen = {(l or "en").lower().strip() for l in languages}
    ordered = [l for l in _LANG_DELIVERY_ORDER if l in seen]
    for lang in sorted(seen):
        if lang not in ordered:
            ordered.append(lang)
    return ordered


def audience_counts_by_language(filters: BroadcastFilters) -> list[dict[str, Any]]:
    f = _normalize_filters(filters)
    with session_scope() as s:
        users = _load_audience_users(s, f)
        if f.language:
            count = sum(1 for user in users if _outbound_channel(user))
            return [{"language": f.language, "label": _language_label(f.language), "count": count}]
        counts: dict[str, int] = defaultdict(int)
        for user in users:
            if not _outbound_channel(user):
                continue
            counts[(user.language or "en").lower()] += 1
        return [
            {"language": lang, "label": _language_label(lang), "count": counts[lang]}
            for lang in _language_delivery_order(list(counts.keys()))
        ]


def audience_whatsapp_session_stats(filters: BroadcastFilters) -> dict[str, int]:
    """How many WhatsApp recipients have an open 24h session vs need a template."""
    from app.whatsapp_session import whatsapp_session_open

    f = _normalize_filters(filters)
    with session_scope() as s:
        users = _load_audience_users(s, f)
        open_count = 0
        closed_count = 0
        for user in users:
            if _outbound_channel(user) != "whatsapp":
                continue
            if whatsapp_session_open(user.phone_e164):
                open_count += 1
            else:
                closed_count += 1
        return {"open_session": open_count, "needs_template": closed_count}


def _delivery_language_batches(
    *,
    language_filter: Optional[str],
    by_lang: dict[str, list[str]],
) -> list[tuple[str, list[str]]]:
    """One batch when a language filter is set; otherwise group by member language."""
    if language_filter:
        lang = language_filter.lower().strip()
        all_ids: list[str] = []
        for msg_ids in by_lang.values():
            all_ids.extend(msg_ids)
        return [(lang, sorted(all_ids))]
    return [(lang, sorted(by_lang[lang])) for lang in _language_delivery_order(list(by_lang.keys()))]


def preview_broadcast_recipients(filters: BroadcastFilters, *, limit: int = 12) -> list[dict[str, str]]:
    f = _normalize_filters(filters)
    with session_scope() as s:
        user_ids = list(s.execute(_audience_user_ids_stmt(f).limit(limit)).scalars().all())
        if not user_ids:
            return []
        users = s.execute(select(User).where(User.id.in_(user_ids)).order_by(User.phone_e164.asc())).scalars().all()
        rows: list[dict[str, str]] = []
        for u in users:
            rows.append(
                {
                    "phone": u.phone_e164,
                    "name": (u.full_name or "").strip() or "—",
                    "channel": u.channel,
                    "language": u.language,
                }
            )
        return rows


def _translate_broadcast_body(body: str, user_lang: str) -> str:
    """Bounded-time translation so broadcast delivery does not hang on Marian load."""
    ul = (user_lang or "en").lower().strip()
    if ul == "en":
        return body
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(translate_outbound_for_user, body, ul)
        try:
            out = fut.result(timeout=_BROADCAST_TRANSLATE_TIMEOUT_SEC)
            return (out or "").strip() or body
        except FuturesTimeoutError:
            _log.warning("Broadcast translation timed out for lang=%s; sending English", ul)
            return body
        except Exception as exc:
            _log.warning("Broadcast translation failed for lang=%s: %s", ul, exc)
            return body


def outbound_body_for_outbox_send(s: Session, msg: MessageOutbox) -> str:
    """Broadcast rows store English; translate per member language when sending."""
    body = (msg.body or "").strip()
    if not msg.broadcast_run_id:
        return body
    run = s.get(BroadcastRun, msg.broadcast_run_id)
    if not run or not run.translate_per_user:
        return body
    return _translate_broadcast_body(body, msg.language)


def _outbound_channel(user: User) -> Optional[DeliverableChannel]:
    ch = (user.channel or "").strip().lower()
    if ch in ("sms", "whatsapp"):
        return ch  # type: ignore[return-value]
    return None


def enqueue_broadcast(
    *,
    filters: BroadcastFilters,
    message_body: str,
    translate_per_user: bool = True,
    created_by: Optional[str] = None,
) -> tuple[str, int, int]:
    """
    Queue one outbox row per matching user. Returns (run_id, audience_count, queued_count).
    Skips users whose channel is not sms/whatsapp.
    """
    body_en = (message_body or "").strip()
    if not body_en:
        raise ValueError("Message body is required.")
    if len(body_en) > 4000:
        raise ValueError("Message is too long (max 4000 characters).")

    f = _normalize_filters(filters)
    now = now_iso()
    run_id = new_id("bcast")
    program_outbox = f.program if f.program != "all" else "broadcast"

    with session_scope() as s:
        users = _load_audience_users(s, f)
        audience_count = len(users)
        queued = 0

        s.add(
            BroadcastRun(
                id=run_id,
                program=f.program,
                enrollment_status=f.enrollment_status,
                channel_filter=f.channel,
                language_filter=f.language,
                message_body=body_en,
                translate_per_user=translate_per_user,
                recipient_count=audience_count,
                queued_count=0,
                created_by=(created_by or "").strip() or None,
                created_at=now,
            )
        )
        s.flush()

        for user in users:
            ch = _outbound_channel(user)
            if not ch:
                continue
            s.add(
                MessageOutbox(
                    id=new_id("msg"),
                    user_id=user.id,
                    program=program_outbox,
                    channel=ch,
                    language=user.language,
                    to_phone_e164=user.phone_e164,
                    body=body_en,
                    due_at=now,
                    status="queued",
                    error=None,
                    broadcast_run_id=run_id,
                    created_at=now,
                    sent_at=None,
                )
            )
            queued += 1

        run = s.get(BroadcastRun, run_id)
        if run:
            run.queued_count = queued

    return run_id, audience_count, queued


def _outbox_programs_for_run(run: BroadcastRun) -> list[str]:
    programs = ["broadcast"]
    prog = (run.program or "").strip().lower()
    if prog and prog != "all" and prog not in programs:
        programs.append(prog)
    return programs


def _link_orphan_outbox_to_run(
    s: Session,
    run: BroadcastRun,
    *,
    queued_only: bool,
) -> int:
    """Attach outbox rows missing broadcast_run_id but created for this run."""
    programs = _outbox_programs_for_run(run)
    clauses = [
        MessageOutbox.broadcast_run_id.is_(None),
        MessageOutbox.program.in_(programs),
        MessageOutbox.created_at == run.created_at,
    ]
    if queued_only:
        clauses.append(MessageOutbox.status == "queued")
    orphans = (
        s.execute(select(MessageOutbox).where(and_(*clauses))).scalars().all()
    )
    for msg in orphans:
        msg.broadcast_run_id = run.id
    if orphans:
        s.flush()
        _log.warning(
            "Broadcast %s: linked %s orphan outbox row(s)",
            run.id,
            len(orphans),
        )
    return len(orphans)


def _queued_rows_for_run(s: Session, run: BroadcastRun) -> list[tuple[str, str]]:
    rid = run.id
    rows = list(
        s.execute(
            select(MessageOutbox.id, MessageOutbox.language).where(
                and_(
                    MessageOutbox.broadcast_run_id == rid,
                    MessageOutbox.status == "queued",
                )
            )
        ).all()
    )
    if not rows:
        _link_orphan_outbox_to_run(s, run, queued_only=True)
        rows = list(
            s.execute(
                select(MessageOutbox.id, MessageOutbox.language).where(
                    and_(
                        MessageOutbox.broadcast_run_id == rid,
                        MessageOutbox.status == "queued",
                    )
                )
            ).all()
        )
    return rows


def _body_for_language_batch(
    *,
    base_body: str,
    language: str,
    translate: bool,
) -> str:
    lang = (language or "en").lower().strip()
    if not translate or lang == "en":
        return base_body
    return _translate_broadcast_body(base_body, lang)


def deliver_broadcast_run(*, run_id: str) -> tuple[int, int, list[dict[str, Any]]]:
    """
    Send queued rows grouped by language (English first, then pcm/ha/ig/yo).
    One translation per language batch. Returns (sent, failed, batch_summaries).
    """
    from app.messaging import send_message, send_whatsapp_broadcast

    rid = (run_id or "").strip()
    if not rid:
        return 0, 0, []

    with session_scope() as s:
        run = s.get(BroadcastRun, rid)
        if not run:
            return 0, 0, []
        base_body = (run.message_body or "").strip()
        translate = bool(run.translate_per_user)
        language_filter = (run.language_filter or "").strip().lower() or None
        queued_rows = _queued_rows_for_run(s, run)

    if not queued_rows:
        _log.warning("Broadcast %s: no queued outbox rows to deliver", rid)
        return 0, 0, []

    by_lang: dict[str, list[str]] = defaultdict(list)
    for msg_id, lang in queued_rows:
        by_lang[(lang or "en").lower().strip()].append(msg_id)

    sent = 0
    failed = 0
    batch_summaries: list[dict[str, Any]] = []
    now = now_iso()

    for lang, msg_ids in _delivery_language_batches(
        language_filter=language_filter, by_lang=by_lang
    ):
        batch_body = _body_for_language_batch(
            base_body=base_body, language=lang, translate=translate
        )
        batch_sent = 0
        batch_failed = 0
        _log.info(
            "Broadcast %s: sending language batch %s (%s recipients)",
            rid,
            lang,
            len(msg_ids),
        )

        for msg_id in msg_ids:
            with session_scope() as s:
                msg = s.get(MessageOutbox, msg_id)
                if not msg or msg.status != "queued":
                    continue
                if msg.channel == "whatsapp":
                    res = send_whatsapp_broadcast(
                        to_phone_e164=msg.to_phone_e164,
                        body=batch_body,
                    )
                else:
                    res = send_message(
                        to_phone_e164=msg.to_phone_e164,
                        body=batch_body,
                        channel=msg.channel,  # type: ignore[arg-type]
                    )
                if res.ok:
                    msg.status = "sent"
                    msg.sent_at = now
                    msg.error = None
                    msg.body = batch_body
                    batch_sent += 1
                    sent += 1
                    _log.info(
                        "Broadcast %s [%s] sent to %s via %s",
                        rid,
                        lang,
                        msg.to_phone_e164,
                        res.via or "unknown",
                    )
                else:
                    msg.status = "failed"
                    msg.error = res.error
                    batch_failed += 1
                    failed += 1
                    _log.error(
                        "Broadcast %s [%s] failed to %s: %s",
                        rid,
                        lang,
                        msg.to_phone_e164,
                        res.error,
                    )

        batch_summaries.append(
            {
                "language": lang,
                "label": _language_label(lang),
                "total": len(msg_ids),
                "sent": batch_sent,
                "failed": batch_failed,
            }
        )

    return sent, failed, batch_summaries


def flush_broadcast_run(*, run_id: str, max_rounds: int = 40, batch_size: int = 50) -> int:
    """Backward-compatible wrapper — delivers the full run."""
    sent, _failed, _batches = deliver_broadcast_run(run_id=run_id)
    return sent


def flush_queued_broadcast(*, max_rounds: int = 40, batch_size: int = 50) -> int:
    """Send all due queued outbox rows (legacy helper)."""
    total = 0
    for _ in range(max_rounds):
        n = send_queued_messages(limit=batch_size)
        total += n
        if n < batch_size:
            break
    return total


def _truncate(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def apply_meta_delivery_status(
    *,
    recipient_digits: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Mark the latest sent WhatsApp outbox row failed when Meta reports delivery failure."""
    from app.util import normalize_phone_e164

    st = (status or "").strip().lower()
    if st not in ("failed", "undelivered"):
        return
    digits = "".join(c for c in (recipient_digits or "") if c.isdigit())
    if not digits:
        return
    phone = normalize_phone_e164(f"+{digits}")
    detail = f"meta_delivery_{st}"
    if error:
        detail = f"{detail}:{error}"
    with session_scope() as s:
        msg = (
            s.execute(
                select(MessageOutbox)
                .where(
                    MessageOutbox.channel == "whatsapp",
                    MessageOutbox.status == "sent",
                    MessageOutbox.to_phone_e164 == phone,
                )
                .order_by(MessageOutbox.sent_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if msg:
            msg.status = "failed"
            msg.error = detail[:500]


def list_recent_broadcast_runs(*, limit: int = 8) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(BroadcastRun).order_by(BroadcastRun.created_at.desc()).limit(limit)).scalars().all()
        out: list[dict[str, Any]] = []
        for run in rows:
            preview = (run.message_body or "").strip()
            out.append(
                {
                    "id": run.id,
                    "created_at": run.created_at,
                    "program_label": _program_label(run.program),
                    "enrollment_status": run.enrollment_status,
                    "channel_label": _channel_label(run.channel_filter),
                    "queued_count": run.queued_count,
                    "recipient_count": run.recipient_count,
                    "created_by": run.created_by,
                    "message_preview": _truncate(preview, 72),
                }
            )
        return out


def get_broadcast_run_detail(run_id: str) -> Optional[dict[str, Any]]:
    rid = (run_id or "").strip()
    if not rid:
        return None
    with session_scope() as s:
        run = s.get(BroadcastRun, rid)
        if not run:
            return None
        linked = (
            s.execute(
                select(func.count())
                .select_from(MessageOutbox)
                .where(MessageOutbox.broadcast_run_id == rid)
            ).scalar()
            or 0
        )
        if linked < (run.queued_count or 0):
            _link_orphan_outbox_to_run(s, run, queued_only=False)
        rows = s.execute(
            select(MessageOutbox, User)
            .outerjoin(User, User.id == MessageOutbox.user_id)
            .where(MessageOutbox.broadcast_run_id == rid)
            .order_by(MessageOutbox.to_phone_e164.asc())
        ).all()
        recipients: list[dict[str, Any]] = []
        for msg, user in rows:
            recipients.append(
                {
                    "outbox_id": msg.id,
                    "phone": msg.to_phone_e164,
                    "name": ((user.full_name if user else "") or "").strip() or "—",
                    "channel": msg.channel,
                    "language": msg.language,
                    "status": msg.status,
                    "error": (msg.error or "").strip() or None,
                    "sent_at": msg.sent_at,
                    "body_preview": _truncate(msg.body, 120),
                }
            )
        sent = sum(1 for r in recipients if r["status"] == "sent")
        failed = sum(1 for r in recipients if r["status"] == "failed")
        queued = sum(1 for r in recipients if r["status"] == "queued")

        language_filter = (run.language_filter or "").strip().lower() or None
        if language_filter:
            language_groups = [
                {
                    "language": language_filter,
                    "label": _language_label(language_filter),
                    "total": len(recipients),
                    "sent": sent,
                    "failed": failed,
                    "queued": queued,
                    "members": recipients,
                }
            ]
        else:
            by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in recipients:
                by_lang[(r["language"] or "en").lower()].append(r)

            language_groups = []
            for lang in _language_delivery_order(list(by_lang.keys())):
                members = by_lang[lang]
                language_groups.append(
                    {
                        "language": lang,
                        "label": _language_label(lang),
                        "total": len(members),
                        "sent": sum(1 for m in members if m["status"] == "sent"),
                        "failed": sum(1 for m in members if m["status"] == "failed"),
                        "queued": sum(1 for m in members if m["status"] == "queued"),
                        "members": members,
                    }
                )

        return {
            "run": {
                "id": run.id,
                "created_at": run.created_at,
                "program": run.program,
                "program_label": _program_label(run.program),
                "enrollment_status": run.enrollment_status,
                "channel_label": _channel_label(run.channel_filter),
                "language_filter": run.language_filter,
                "message_body": run.message_body,
                "translate_per_user": run.translate_per_user,
                "recipient_count": run.recipient_count,
                "queued_count": run.queued_count,
                "created_by": run.created_by,
            },
            "recipients": recipients,
            "language_groups": language_groups,
            "stats": {
                "total": len(recipients),
                "sent": sent,
                "failed": failed,
                "queued": queued,
            },
        }


def _program_label(code: str) -> str:
    if code == "all":
        return "All programmes"
    labels = {
        "maternal_child": "Maternal & child health",
        "chronic": "Chronic diseases",
        "youth": "Youth health",
        "kidney": "Dialysis & kidney",
    }
    return labels.get(code, code)


def _channel_label(code: Optional[str]) -> str:
    if not code:
        return "SMS + WhatsApp"
    return {"sms": "SMS only", "whatsapp": "WhatsApp only"}.get(code, code)
