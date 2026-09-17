"""OPay: hosted Cashier checkout; webhook (body-signed) confirms payment."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from app.integration_config import get_effective_integration

_log = logging.getLogger(__name__)

_LIVE_BASE = "https://liveapi.opaycheckout.com"
_TEST_BASE = "https://testapi.opaycheckout.com"


def _api_base() -> str:
    # OPay does not distinguish sandbox/live by key prefix (both a merchant's test and
    # production accounts use plain keys); the environment is selected by base URL.
    # Set HC_OPAY_ENV=sandbox while testing against a test-mode OPay merchant account.
    env = (os.getenv("HC_OPAY_ENV") or "").strip().lower()
    return _TEST_BASE if env in ("sandbox", "test", "staging") else _LIVE_BASE


@dataclass
class TelehealthPaymentSetup:
    reference: str
    customer_email: str
    checkout_url: Optional[str] = None
    error: Optional[str] = None


def is_configured() -> bool:
    cfg = get_effective_integration()
    return bool(
        (cfg.opay_secret_key or "").strip()
        and (cfg.opay_public_key or "").strip()
        and (cfg.opay_merchant_id or "").strip()
    )


def _secret() -> str:
    return (get_effective_integration().opay_secret_key or "").strip()


def _public_key() -> str:
    return (get_effective_integration().opay_public_key or "").strip()


def _merchant_id() -> str:
    return (get_effective_integration().opay_merchant_id or "").strip()


def _opay_headers() -> dict[str, str]:
    # Cashier Create Payment and Cashier Payment Status both authenticate with the merchant's
    # PUBLIC key as a plain bearer token — no request signing. (OPay's HMAC-SHA512 request
    # signing scheme is only for the separate "Server Side APIs" family, which this integration
    # does not use.)
    return {
        "Authorization": f"Bearer {_public_key()}",
        "MerchantId": _merchant_id(),
        "Content-Type": "application/json",
    }


def verify_webhook_signature(event: dict[str, Any]) -> bool:
    """Verify an OPay callback body. `event` is the parsed JSON payload."""
    secret = _secret()
    if not secret:
        return False
    received = str(event.get("sha512") or "").strip()
    if not received:
        return False
    data = event.get("payload") or {}
    refunded = "t" if bool(data.get("refunded")) else "f"
    auth_str = (
        '{{Amount:"{amount}",Currency:"{currency}",Reference:"{reference}",'
        "Refunded:{refunded},Status:\"{status}\",Timestamp:\"{timestamp}\","
        'Token:"{token}",TransactionID:"{transaction_id}"}}'
    ).format(
        amount=data.get("amount", ""),
        currency=data.get("currency", ""),
        reference=data.get("reference", ""),
        refunded=refunded,
        status=data.get("status", ""),
        timestamp=data.get("timestamp", ""),
        token=data.get("token", ""),
        transaction_id=data.get("transactionId", ""),
    )
    digest = hmac.new(secret.encode("utf-8"), auth_str.encode("utf-8"), hashlib.sha3_512).hexdigest()
    return hmac.compare_digest(digest, received)


def payment_reference_for_session(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:100]


def _opay_checkout_email(phone_e164: str) -> str:
    digits = "".join(c for c in (phone_e164 or "") if c.isdigit()) or "0"
    cfg = get_effective_integration()
    host = "kareconnect.karevault.com"
    base = (cfg.hc_base_url or "").strip()
    if base.startswith("http"):
        parsed = urlparse(base)
        if parsed.hostname:
            host = parsed.hostname
    return f"patient+{digits}@{host}"


def _opay_post(url: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any], Optional[str]]:
    try:
        resp = requests.post(url, json=payload, headers=_opay_headers(), timeout=25)
    except requests.RequestException as exc:
        return False, {}, str(exc)
    try:
        body = resp.json()
    except ValueError:
        return False, {}, f"OPay HTTP {resp.status_code}: invalid JSON"
    if not (200 <= resp.status_code < 300) or body.get("code") != "00000":
        msg = body.get("message") or (resp.text or "")[:300]
        return False, body, f"OPay HTTP {resp.status_code}: {msg}"
    return True, body, None


def verify_transaction(reference: str) -> tuple[bool, str, dict[str, Any]]:
    """Confirm an OPay payment via Query Payment Status. Returns (paid, detail, data)."""
    ref = (reference or "").strip()
    if not ref:
        return False, "Missing payment reference.", {}
    if not is_configured():
        return False, "OPay keys not configured.", {}
    ok, body, err = _opay_post(f"{_api_base()}/api/v1/international/cashier/status", {"reference": ref, "country": "NG"})
    if not ok:
        return False, f"OPay verify failed: {err}", body
    status = str(body.get("status") or "").strip().upper()
    if status == "SUCCESS":
        return True, "Payment verified with OPay.", body
    return False, f"OPay transaction status: {status or 'unknown'}", body


def initialize_checkout_payment(
    *,
    session_id: str,
    phone_e164: str,
    amount_ngn: int,
    patient_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Hosted OPay Cashier checkout. Webhook confirms payment. Returns (cashier_url, reference, error)."""
    amount = max(0, int(amount_ngn or 0))
    if amount <= 0:
        return None, None, "Amount must be greater than zero"
    reference = payment_reference_for_session(session_id)
    email = _opay_checkout_email(phone_e164)
    cfg = get_effective_integration()
    base = (cfg.hc_base_url or "").rstrip("/")
    return_url = (
        f"{base}/telehealth/payment/complete?session_id={session_id}"
        if base.startswith("https://")
        else "https://kareconnect.karevault.com/telehealth/payment/complete"
    )
    payload: dict[str, Any] = {
        "reference": reference,
        "country": "NG",
        "amount": {"total": amount * 100, "currency": "NGN"},
        "returnUrl": return_url,
        "callbackUrl": f"{base}/webhooks/opay" if base.startswith("https://") else None,
        "product": {
            "name": "Kareconnect telehealth consultation",
            "description": f"Telehealth session {session_id}",
        },
        "userInfo": {
            "userEmail": email,
            "userMobile": phone_e164,
            "userName": (patient_name or "Patient").strip() or "Patient",
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    ok, body, err = _opay_post(f"{_api_base()}/api/v1/international/cashier/create", payload)
    if not ok:
        return None, None, err
    data = body.get("data") or {}
    url = data.get("cashierUrl")
    ref = data.get("reference") or reference
    if not url:
        return None, None, "OPay response missing cashierUrl"
    return str(url), str(ref), None


def create_telehealth_payment(
    *,
    session_id: str,
    phone_e164: str,
    amount_ngn: int,
    patient_name: Optional[str] = None,
) -> TelehealthPaymentSetup:
    """OPay hosted checkout link. Payment completion is detected via POST /webhooks/opay."""
    reference = payment_reference_for_session(session_id)
    email = _opay_checkout_email(phone_e164)
    if not is_configured():
        return TelehealthPaymentSetup(
            reference=reference,
            customer_email=email,
            error="OPay keys not configured",
        )

    url, ref, err = initialize_checkout_payment(
        session_id=session_id,
        phone_e164=phone_e164,
        amount_ngn=amount_ngn,
        patient_name=patient_name,
    )
    if url:
        return TelehealthPaymentSetup(
            reference=ref or reference,
            customer_email=email,
            checkout_url=url,
        )
    return TelehealthPaymentSetup(
        reference=reference,
        customer_email=email,
        error=err or "OPay payment setup failed",
    )


def extract_session_id_from_event(event: dict[str, Any]) -> Optional[str]:
    data = event.get("payload") or {}
    ref = data.get("reference")
    if ref:
        return str(ref)
    return None


def test_opay_initialize() -> tuple[bool, str]:
    if not is_configured():
        return False, "OPay keys not set (OPAY_SECRET_KEY / OPAY_PUBLIC_KEY / OPAY_MERCHANT_ID in env or admin)."
    setup = create_telehealth_payment(
        session_id="ths_opay_selftest",
        phone_e164="+2348000000000",
        amount_ngn=100,
        patient_name="Test Patient",
    )
    if setup.error and not setup.checkout_url:
        return False, setup.error
    lines = [f"Customer email: {setup.customer_email}", f"Reference: {setup.reference}"]
    if setup.checkout_url:
        lines.append("Checkout URL generated.")
    lines.append(
        "Webhook required: set OPay dashboard callback URL → https://kareconnect.karevault.com/webhooks/opay "
        "(payment status callback confirms payment and sends call instructions)."
    )
    return True, " ".join(lines)
