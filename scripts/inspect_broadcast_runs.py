"""One-off: print recent broadcast runs and outbox statuses (Railway SSH)."""
from collections import Counter

from sqlalchemy import desc, select

from app.database import session_scope
from app.models import BroadcastRun, MessageOutbox


def main() -> None:
    with session_scope() as s:
        runs = (
            s.execute(select(BroadcastRun).order_by(desc(BroadcastRun.created_at)).limit(8))
            .scalars()
            .all()
        )
        if not runs:
            print("No broadcast runs found.")
            return
        for r in runs:
            msgs = (
                s.execute(select(MessageOutbox).where(MessageOutbox.broadcast_run_id == r.id))
                .scalars()
                .all()
            )
            print(
                f"RUN {r.id} {r.created_at} queued={r.queued_count} "
                f"recipients={r.recipient_count} lang={r.language_filter} ch={r.channel_filter}"
            )
            print("  statuses", dict(Counter(m.status for m in msgs)))
            for m in msgs[:8]:
                err = (m.error or "")[:200]
                print(f"  {m.to_phone_e164} {m.channel} {m.language} {m.status} {err!r}")


if __name__ == "__main__":
    main()
