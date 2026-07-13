"""Paystack: dedicated virtual accounts (bank transfer) + checkout fallback; webhooks confirm payment."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests
from sqlalchemy import select

from app.database import session_scope
from app.integration_config import get_effective_integration
from app.models import TelehealthSession

_log = logging.getLogger(__name__)

_PAYSTACK_CHANNELS = ["bank", "bank_transfer", "card", "ussd", "qr", "mobile_money"]


@dataclass
class TelehealthPaymentSetup:
    reference: str
    customer_email: str
    checkout_url: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    error: Optional[str] = None


def is_configured() -> bool:
    secret = _secret()
    return bool(secret) and secret.startswith("sk_")


def _secret() -> str:
    return (get_effective_integration().paystack_secret_key or "").strip()


def _preferred_bank() -> str:
    explicit = (os.getenv("HC_PAYSTACK_PREFERRED_BANK") or "").strip()
    if explicit:
        return explicit
    return "test-bank" if _secret().startswith("sk_test_") else "wema-bank"


def _paystack_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_secret()}", "Content-Type": "application/json"}


def verify_webhook_signature(body: bytes, x_paystack_signature: Optional[str]) -> bool:
    secret = _secret()
    if not secret:
        return False
    if not x_paystack_signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, x_paystack_signature.strip())


def verify_transaction(reference: str) -> tuple[bool, str, dict[str, Any]]:
    """
    Confirm a Paystack charge via GET /transaction/verify/:reference.
    Returns (paid, detail_message, paystack_data).
    """
    ref = (reference or "").strip()
    if not ref:
        return False, "Missing payment reference.", {}
    if not is_configured():
        return False, "Paystack secret key not configured.", {}
    try:
        resp = requests.get(
            f"https://api.paystack.co/transaction/verify/{quote(ref, safe='')}",
            headers=_paystack_headers(),
            timeout=25,
        )
    except requests.RequestException as exc:
        return False, f"Paystack verify request failed: {exc}", {}
    try:
        body = resp.json()
    except ValueError:
        return False, f"Paystack verify HTTP {resp.status_code}: invalid JSON", {}
    if not (200 <= resp.status_code < 300) or not body.get("status"):
        msg = body.get("message") or (resp.text or "")[:300]
        return False, f"Paystack verify failed: {msg}", {}
    data = body.get("data") or {}
    status = (data.get("status") or "").strip().lower()
    if status == "success":
        return True, "Payment verified with Paystack.", data
    return False, f"Paystack transaction status: {status or 'unknown'}", data


def payment_reference_for_session(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:100]


def _paystack_checkout_email(phone_e164: str) -> str:
    digits = "".join(c for c in (phone_e164 or "") if c.isdigit()) or "0"
    cfg = get_effective_integration()
    host = "kareconnect.karevault.com"
    base = (cfg.hc_base_url or "").strip()
    if base.startswith("http"):
        parsed = urlparse(base)
        if parsed.hostname:
            host = parsed.hostname
    return f"patient+{digits}@{host}"


def _split_patient_name(full_name: Optional[str]) -> tuple[str, str]:
    name = (full_name or "").strip()
    if not name:
        return "Patient", "Kareconnect"
    parts = name.split(None, 1)
    if len(parts) == 1:
        return parts[0], "Patient"
    return parts[0], parts[1]


def _paystack_post(path: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any], Optional[str]]:
    try:
        resp = requests.post(
            f"https://api.paystack.co{path}",
            json=payload,
            headers=_paystack_headers(),
            timeout=25,
        )
    except requests.RequestException as exc:
        return False, {}, str(exc)
    try:
        body = resp.json()
    except ValueError:
        return False, {}, f"Paystack HTTP {resp.status_code}: invalid JSON"
    if not (200 <= resp.status_code < 300) or not body.get("status"):
        msg = body.get("message") or (resp.text or "")[:300]
        return False, body, f"Paystack HTTP {resp.status_code}: {msg}"
    return True, body, None


def _create_paystack_customer(
    *,
    email: str,
    phone_e164: str,
    first_name: str,
    last_name: str,
    session_id: str,
) -> tuple[Optional[str], Optional[str]]:
    ok, body, err = _paystack_post(
        "/customer",
        {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone_e164,
            "metadata": {"session_id": session_id, "source": "kareconnect_telehealth"},
        },
    )
    if ok:
        code = (body.get("data") or {}).get("customer_code")
        return (str(code) if code else None), None
    if err and "already exists" in err.lower():
        return None, None
    return None, err


def create_dedicated_virtual_account(
    *,
    session_id: str,
    phone_e164: str,
    patient_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Paystack DVA (account generation) for bank transfer.
    Returns (account_number, bank_name, account_name, customer_email, error).
    """
    email = _paystack_checkout_email(phone_e164)
    first, last = _split_patient_name(patient_name)

    ok, body, err = _paystack_post(
        "/dedicated_account/assign",
        {
            "email": email,
            "first_name": first,
            "last_name": last,
            "phone": phone_e164,
            "preferred_bank": _preferred_bank(),
            "country": "NG",
        },
    )
    if ok:
        data = body.get("data") or {}
        bank = data.get("bank") or {}
        acct = str(data.get("account_number") or "") or None
        if acct:
            return (
                acct,
                str(bank.get("name") or "") or None,
                str(data.get("account_name") or "") or None,
                email,
                None,
            )
        _log.info("Paystack DVA assign async session=%s email=%s", session_id, email)
        return None, None, None, email, None
    if err:
        _log.warning("Paystack DVA assign failed session=%s: %s", session_id, err)

    customer_code, _ = _create_paystack_customer(
        email=email,
        phone_e164=phone_e164,
        first_name=first,
        last_name=last,
        session_id=session_id,
    )
    if customer_code:
        ok2, body2, err2 = _paystack_post(
            "/dedicated_account",
            {"customer": customer_code, "preferred_bank": _preferred_bank()},
        )
        if ok2:
            data = body2.get("data") or {}
            bank = data.get("bank") or {}
            return (
                str(data.get("account_number") or "") or None,
                str(bank.get("name") or "") or None,
                str(data.get("account_name") or "") or None,
                email,
                None,
            )
        err = err2 or err

    return None, None, None, email, err or "Could not create Paystack virtual account"


def initialize_checkout_payment(
    *,
    session_id: str,
    phone_e164: str,
    amount_ngn: int,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Hosted Paystack checkout (card/USSD/transfer). Webhook charge.success confirms payment."""
    amount = max(0, int(amount_ngn or 0))
    if amount <= 0:
        return None, None, "Amount must be greater than zero"
    reference = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:100]
    email = _paystack_checkout_email(phone_e164)
    cfg = get_effective_integration()
    base = (cfg.hc_base_url or "").rstrip("/")
    payload: dict[str, Any] = {
        "email": email,
        "amount": amount * 100,
        "currency": "NGN",
        "reference": reference,
        "channels": _PAYSTACK_CHANNELS,
        "metadata": {
            "session_id": session_id,
            "patient_phone_e164": phone_e164,
            "custom_fields": [
                {"display_name": "Telehealth session", "variable_name": "session_id", "value": session_id},
            ],
        },
    }
    if base.startswith("https://"):
        payload["callback_url"] = f"{base}/telehealth/payment/complete?session_id={session_id}"

    ok, body, err = _paystack_post("/transaction/initialize", payload)
    if not ok:
        return None, None, err
    data = body.get("data") or {}
    url = data.get("authorization_url")
    ref = data.get("reference") or reference
    if not url:
        return None, None, "Paystack response missing authorization_url"
    return str(url), str(ref), None


def create_telehealth_payment(
    *,
    session_id: str,
    phone_e164: str,
    amount_ngn: int,
    patient_name: Optional[str] = None,
) -> TelehealthPaymentSetup:
    """
    Primary: Paystack dedicated virtual account (bank transfer).
    Fallback: hosted checkout link.
    Payment completion is detected via POST /webhooks/paystack (charge.success).
    """
    reference = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:100]
    if not is_configured():
        return TelehealthPaymentSetup(
            reference=reference,
            customer_email=_paystack_checkout_email(phone_e164),
            error="Paystack secret key not configured",
        )

    acct, bank, acct_name, email, dva_err = create_dedicated_virtual_account(
        session_id=session_id,
        phone_e164=phone_e164,
        patient_name=patient_name,
    )
    if acct and bank:
        _log.info("Paystack DVA ready session=%s account=%s", session_id, acct)
        return TelehealthPaymentSetup(
            reference=reference,
            customer_email=email,
            account_number=acct,
            bank_name=bank,
            account_name=acct_name,
        )

    url, ref, checkout_err = initialize_checkout_payment(
        session_id=session_id,
        phone_e164=phone_e164,
        amount_ngn=amount_ngn,
    )
    if url:
        return TelehealthPaymentSetup(
            reference=ref or reference,
            customer_email=email,
            checkout_url=url,
            account_number=acct,
            bank_name=bank,
            account_name=acct_name,
        )

    return TelehealthPaymentSetup(
        reference=reference,
        customer_email=email,
        error=dva_err or checkout_err or "Paystack payment setup failed",
    )


def extract_session_id_from_event(event: dict[str, Any]) -> Optional[str]:
    data = event.get("data") or {}
    meta = data.get("metadata") or {}
    sid = meta.get("session_id")
    if sid:
        return str(sid)
    custom = meta.get("custom_fields") or []
    for row in custom:
        if isinstance(row, dict) and row.get("variable_name") == "session_id":
            val = row.get("value")
            if val:
                return str(val)
    ref = data.get("reference")
    if ref and str(ref).startswith("ths_"):
        return str(ref)
    return resolve_session_id_from_charge(data)


def resolve_session_id_from_charge(data: dict[str, Any]) -> Optional[str]:
    """Match DVA bank-transfer charges to a pending telehealth session."""
    customer = data.get("customer") or {}
    email = ""
    if isinstance(customer, dict):
        email = (customer.get("email") or "").strip()
    if not email:
        email = (data.get("customer_email") or data.get("email") or "").strip()
    amount_kobo = data.get("amount")
    try:
        amount_ngn = int(amount_kobo) // 100 if amount_kobo is not None else None
    except (TypeError, ValueError):
        amount_ngn = None

    with session_scope() as s:
        if email:
            row = s.execute(
                select(TelehealthSession)
                .where(
                    TelehealthSession.paystack_customer_email == email,
                    TelehealthSession.billing_status == "pending",
                )
                .order_by(TelehealthSession.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row:
                return row.id
        if amount_ngn is not None:
            row = s.execute(
                select(TelehealthSession)
                .where(
                    TelehealthSession.billing_status == "pending",
                    TelehealthSession.fee_ngn == amount_ngn,
                )
                .order_by(TelehealthSession.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row:
                return row.id
    return None


def apply_dedicated_account_assigned(event: dict[str, Any]) -> Optional[str]:
    """Handle dedicatedaccount.assign.success — store generated account on pending session."""
    data = event.get("data") or {}
    customer = data.get("customer") or {}
    email = (customer.get("email") or "").strip()
    account_number = (data.get("account_number") or "").strip()
    bank = data.get("bank") or {}
    bank_name = (bank.get("name") or "").strip()
    account_name = (data.get("account_name") or "").strip()
    if not email or not account_number:
        return None

    with session_scope() as s:
        session = s.execute(
            select(TelehealthSession)
            .where(
                TelehealthSession.paystack_customer_email == email,
                TelehealthSession.billing_status == "pending",
            )
            .order_by(TelehealthSession.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not session:
            return None
        session.payment_account_number = account_number
        session.payment_bank_name = bank_name or None
        session.payment_account_name = account_name or None
        from app.util import now_iso

        session.updated_at = now_iso()
        return session.id


def test_paystack_initialize() -> tuple[bool, str]:
    if not is_configured():
        return False, "Paystack secret key not set (sk_test_… / sk_live_… in env or admin)."
    setup = create_telehealth_payment(
        session_id="ths_paystack_selftest",
        phone_e164="+2348000000000",
        amount_ngn=100,
        patient_name="Test Patient",
    )
    lines = [f"Customer email: {setup.customer_email}", f"Reference: {setup.reference}"]
    if setup.account_number:
        lines.append(f"DVA OK: {setup.bank_name} {setup.account_number} ({setup.account_name})")
    if setup.checkout_url:
        lines.append("Checkout URL generated (fallback).")
    if setup.error and not setup.account_number and not setup.checkout_url:
        return False, setup.error
    lines.append(
        "Webhook required: set Paystack dashboard → https://kareconnect.karevault.com/webhooks/paystack "
        "(charge.success sends call instructions after payment)."
    )
    return True, " ".join(lines)
