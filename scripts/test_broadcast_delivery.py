#!/usr/bin/env python3
"""Smoke-test WhatsApp broadcast delivery against production DB/API."""
from __future__ import annotations

import sys

from app.broadcast import BroadcastFilters, deliver_broadcast_run, enqueue_broadcast, get_broadcast_run_detail
from app.messaging import send_whatsapp_broadcast
from app.whatsapp_session import whatsapp_session_open


def _test_direct_send(phone: str) -> None:
    print(f"\n=== direct send to {phone} (session open={whatsapp_session_open(phone)}) ===")
    res = send_whatsapp_broadcast(
        to_phone_e164=phone,
        body="Kareconnect broadcast smoke test — direct send.",
    )
    print("result:", res)


def _test_broadcast_run(*, channel: str | None, language: str | None) -> str:
    filters = BroadcastFilters(
        program="all",
        enrollment_status="active",
        channel=channel,
        language=language,
    )
    body = "Kareconnect broadcast smoke test — automated delivery check."
    run_id, audience, queued = enqueue_broadcast(
        filters=filters,
        message_body=body,
        translate_per_user=False,
        created_by="scripts/test_broadcast_delivery.py",
    )
    print(f"\n=== broadcast run {run_id} audience={audience} queued={queued} ===")
    sent, failed, batches = deliver_broadcast_run(run_id=run_id)
    print(f"deliver: sent={sent} failed={failed} batches={batches}")
    detail = get_broadcast_run_detail(run_id)
    if detail:
        print("stats:", detail["stats"])
        for row in detail["recipients"]:
            print(f"  {row['phone']} {row['status']} {(row['error'] or '')[:100]}")
    return run_id


def main() -> int:
    # User who messaged recently on WhatsApp
    _test_direct_send("+2347071601220")
    _test_broadcast_run(channel="whatsapp", language="en")
    return 0


if __name__ == "__main__":
    sys.exit(main())
