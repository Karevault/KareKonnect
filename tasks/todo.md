# Replace Paystack with OPay for telehealth payments

Full plan: see conversation / `C:\Users\BIggest\.claude\plans\quizzical-wobbling-kernighan.md`.
Approach: checkout-only via OPay Cashier (no DVA equivalent), full replace of Paystack.

## Todo

- [x] Write `app/opay_billing.py` (signing, cashier create, status query, webhook verify, `create_telehealth_payment`, `test_opay_initialize`)
- [x] Delete `app/paystack_billing.py`
- [x] Update `app/telehealth.py` (imports, `_finalize_new_booking_impl`, `_call_instructions`, delete `deliver_payment_account_message`, `confirm_payment_from_browser_callback`, `apply_opay_webhook_event`)
- [x] Update `app/main.py` (`/webhooks/opay` route, body-based signature check, `/telehealth/payment/complete` copy)
- [x] Update `app/models.py` (add `opay_secret_key`/`opay_public_key`/`opay_merchant_id` to `IntegrationSettings`)
- [x] Update `app/integration_config.py` (effective-integration dataclass fields)
- [x] Update `app/config.py` (env-backed settings)
- [x] Update `app/db.py` (new `_ensure_integration_opay_columns()` migration)
- [x] Update `app/admin.py` (self-test route/button, integration settings form fields)
- [x] Update `app/integration_self_test.py` (`test_opay()`)
- [x] Update `app/templates/sqladmin/webhooks_reference.html` and `_integration_self_test_panel.html`
- [x] Update `.env.example`
- [x] Update `docs/FEATURES.md`, `docs/FEATURES-SIMPLE.md`
- [x] Update `tests/test_karevault_telehealth.py` mock path
- [x] Run `python -m pytest tests/test_karevault_telehealth.py -q` (25 passed)
- [x] `grep -ri paystack app/ tests/ docs/ .env.example` — only the intentionally-kept deprecated DB columns in `app/models.py`/`app/db.py` remain
- [x] Review section below

## Review

**What changed:** Paystack (DVA + checkout) fully replaced with OPay's hosted Cashier checkout across billing logic, routes, DB config, admin UI, self-test, docs, and env template. New module `app/opay_billing.py` replaces `app/paystack_billing.py` 1:1 in call sites but is simpler: no dedicated-virtual-account flow, no email/amount fuzzy-matching for webhook resolution (OPay always echoes our own reference back).

**Verification performed:**
- `python -m pytest tests/ -q --ignore=tests/test_session_isolation.py` → 85 passed (the ignored file fails to import due to a pre-existing, unrelated missing `qrcode` package — confirmed present before this change via `git stash`).
- All edited modules parse (`ast.parse`) and the core changed modules (`app.opay_billing`, `app.telehealth`, `app.integration_config`, `app.integration_self_test`, `app.db`, `app.models`) import cleanly.
- Repo-wide `grep -ri paystack` clean except the two deliberately-kept deprecated DB columns.

**Not yet verified (needs real OPay sandbox access — flagged in the plan):**
- The exact webhook callback payload field names/casing used in `verify_webhook_signature` (particularly the `Token` field) are reconstructed from OPay's documentation pages, not a captured raw payload. This must be checked against a real sandbox callback before trusting it in production — a wrong signature format either silently drops real payments or accepts forged ones.
- End-to-end Cashier checkout call against OPay sandbox (`HC_OPAY_ENV=sandbox`) has not been exercised live — only reviewed against the documented request/response shape.
- No UI/browser testing was done (backend/API-only change); the admin "Test OPay" self-test button and `/telehealth/payment/complete` page should be clicked through once real sandbox keys are set.
