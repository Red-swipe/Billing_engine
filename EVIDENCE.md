# Section 6 Acceptance Evidence

This document is an examiner-facing acceptance checklist for the current
repository. The current full regression result is **80 passed**. Claims below
are limited to committed implementation, automated tests, and the recorded
real Stripe test-mode probe.

## Item 4 acceptance - tenant API-key isolation

| Check | Result | Evidence |
|---|---|---|
| Missing and invalid keys rejected | PASS | `tests/test_auth.py::test_missing_and_invalid_api_keys_are_rejected` |
| Valid key succeeds | PASS | `tests/test_auth.py::test_valid_key_succeeds_and_usage_is_isolated` |
| Cross-tenant `/usage` rejected | PASS | Same test uses two tenants and two keys |
| Cross-tenant `/generate` rejected | PASS | `tests/test_auth.py::test_generate_cannot_cross_tenant_boundary` |
| Cross-tenant checkout rejected | PASS | `tests/test_auth.py::test_checkout_cannot_cross_tenant_boundary` |
| Webhook remains independently authenticated | PASS | `tests/test_auth.py::test_webhook_remains_signature_authenticated_only` |
| Full regression | PASS | `80 passed` |

Tenant API keys are stored only as SHA-256 hashes. Isolation tests use different
tenant IDs and different API keys. Existing databases retain the repository's
pre-existing migration limitation: this project uses `create_all` and has no
Alembic history.

## Section 6 Checklist

| Requirement | Status | Evidence / Test | Observed result |
|---|---|---|---|
| 1. One usage event per action, deduped by key | PASS | test_generate_creates_exactly_one_usage_event; test_same_key_twice_returns_identical_response_and_one_row; test_concurrent_identical_keys_create_exactly_one_event | One successful action creates one usage_events row. Replays return the stored result and do not add a row. |
| 2. Quota checked before action | PASS | test_api_quota_boundary_at_limit; test_token_quota_boundary_at_limit; app/services/usage_service.py | Quota is checked before insert. Rejected requests create zero usage rows and leave the rejected key retryable. |
| 3. 429 / 402 clear error responses | PARTIAL | test_api_quota_boundary_at_limit; test_inactive_subscription_is_402_and_creates_no_event; test_402_is_distinct_from_429; app/routes/generate.py | 429 and 402 return structured JSON with distinct error codes and messages. No Retry-After header is implemented on 429, so header-based retry guidance is incomplete. |
| 4. Monthly usage rolls into cost | PARTIAL | test_usage_reports_monthly_cost_and_excludes_previous_month; GET /usage/{tenant_id} implementation | Monthly token usage is rolled into integer cost_cents, with previous-month rows excluded. API-call pricing is not included in cost_cents. |
| 5. Token pricing rules | PASS | tests/test_stage_d.py: test_calculate_cost_exact_rates_and_fractional_units, test_calculate_cost_mixed_and_reasoning_is_not_double_counted, test_calculate_cost_rounds_half_up | Input is $0.00025/1k, cached input is $0.000025/1k, output is $0.00075/1k; reasoning is charged once at the output rate; results are integer cents rounded half-up. |
| 6. Pricing pinned in configuration | PARTIAL | app/services/pricing.py; app/config.py; pricing tests | Rates are centralized as constants in app/services/pricing.py, but they are not configuration values in app/config.py. |
| 7. Checkout works end-to-end in Stripe test mode | PASS | Recorded real Stripe test-mode probe; Stripe CLI forwarding evidence | Real Stripe Checkout completed successfully for Tenant 2 in test mode. |
| 8. Webhooks verify signatures | PASS | Real forged-signature probe; test_forged_signature_returns_400_and_writes_nothing; test_signature_from_wrong_secret_is_rejected | Forged signature returned HTTP 400 and changed no database state. |
| 9. Webhooks deduplicate events | PASS | Real replay probe; test_replay_same_event_twice_processes_once; test_duplicate_delivery_does_not_reapply_business_operation | Replays returned HTTP 200 duplicate. Exactly one stripe_events row existed and no duplicate subscription was created. |
| 10. Webhooks update the subscription/plan | PASS | Real checkout.session.completed probe; test_checkout_session_completed_upgrades_to_pro; test_subscription_updated_syncs_state; test_subscription_deleted_marks_inactive_and_blocks_generate | The real completed-checkout event changed Tenant 2 to Pro with an active subscription. customer.subscription.updated was not observed during the real checkout probe; lifecycle behavior is covered by automated tests. |
| 11. Real persistence | PASS | SQLite models/database; fresh-clone evidence; real Stripe probe | Plans, tenants, usage events, Stripe events, customers, and subscriptions persist in SQLite across requests. |
| 12. Tenant data isolation | PASS | `tests/test_auth.py`; `app/auth.py`; protected generate/usage/checkout routes | Tenant API keys are hashed, authenticated tenants are authoritative, and cross-tenant body/URL access is rejected. |
| 13. Required database/index behavior | PASS | app/models.py; test_concurrent_identical_keys_create_exactly_one_event; SQLite schema evidence | Primary keys, foreign keys, unique email/customer/event/idempotency constraints, and indexes for tenant/event timestamps are present. |
| 14. Layered architecture | PASS | app/routes, app/services, app/models.py, app/database.py | FastAPI routes handle transport/validation, services handle metering/pricing/Stripe logic, and SQLAlchemy models/database handle persistence. |
| 15. Validation / clean 4xx responses | PASS | test_invalid_payloads_rejected_without_creating_events; test_unknown_tenant_is_404; checkout/webhook negative-path tests | Invalid payloads are rejected without usage rows; unknown tenants return 404; inactive subscriptions return 402; quota exhaustion returns 429; invalid webhook signatures return 400. |
| 16. Idempotency | PASS | Metering replay tests and Stripe replay tests listed above | Client-supplied X-Idempotency-Key makes metering replay the original status/body; Stripe event IDs make webhook processing exactly-once. |
| 17. Secrets hygiene | PASS | app/config.py; checkout secret-response test; repository history/status checks | Stripe credentials are loaded from environment variables, the secret key is not returned by checkout, and runtime secret/database/log files were not staged in the checkpoint commits. |
| 18. Background job with retries/failure alert | PASS | `tests/test_background_jobs.py`; `app/services/background_jobs.py` | Usage-cost reconciliation is scheduled with FastAPI BackgroundTasks, retries are bounded/configurable, and one structured alert is emitted only after final failure. |
| 19. Schema migrations | NOT IMPLEMENTED | app/main.py; requirements.txt; DESIGN.md | Alembic is listed as a dependency but unused. Startup uses Base.metadata.create_all; no migration history exists. |

## Real Stripe Evidence

Recorded from the real Stripe test-mode checkout and webhook probe:

~~~text
Real Stripe Checkout: PASS
Payment completed: YES
Tenant: 2
Price: £10.00 GBP/month
checkout.session.completed: PASS
Real event ID: evt_1ULQ2iCSRoP1ezLYuediQy1r
Webhook response: HTTP 200
Stripe customer persisted: YES
Subscription persisted: YES
Plan: Pro
Subscription status: active
GET /usage/2: HTTP 200
~~~

The live Stripe CLI observed checkout.session.completed and forwarded it to
POST /webhooks/stripe. customer.subscription.updated was not observed during
this real checkout probe; it is covered by automated tests only.

### Forged Signature

~~~text
HTTP 400: PASS
Database unchanged: PASS
~~~

The exact stored payload for the real event was sent with an intentionally
invalid signature. The event count and Tenant 2 subscription/plan state were
unchanged.

### Replay / Deduplication

~~~text
Original live delivery: processed
Replay: HTTP 200 duplicate
Exactly one stripe_events row: PASS
Duplicate subscription: NO
~~~

The original live delivery processed the checkout event. Subsequent exact
replays were acknowledged as duplicates, left one stripe_events row, and did
not create a second subscription.

### Post-checkout Redirect Note

After successful payment and webhook processing, Stripe redirected to:

~~~text
http://localhost:8000/
~~~

The root route is not implemented, so it returned HTTP 404. This happened
after successful payment and after the webhook had upgraded Tenant 2 to Pro; it
did not prevent checkout completion or subscription persistence.

## Acceptance Probes

### Probe 1 - Idempotency

The same X-Idempotency-Key was submitted twice to POST /generate. Both
responses were HTTP 200 with identical response bytes and the same
usage_event_id; exactly one usage row existed. Evidence:
test_same_key_twice_returns_identical_response_and_one_row,
test_first_and_replay_response_bytes_are_identical, and
test_first_response_matches_stored_body_exactly.

### Probe 2 - Quota Boundary

The API-call boundary probe established the following observed behavior:

~~~text
999 existing calls + 1 request: HTTP 200
1000 existing calls + 1 request: HTTP 429
Rejected request creates zero usage rows: PASS
~~~

The same boundary behavior is covered by test_api_quota_boundary_at_limit.
Token quota behavior is covered by test_token_quota_boundary_at_limit.

### Probe 3 - Real Stripe

Free Tenant 2 used real Stripe test-mode Checkout, completed payment, received
checkout.session.completed, and was updated to an active Pro subscription.
The persisted customer and subscription were confirmed, and GET /usage/2
returned HTTP 200 with plan Pro.

### Probe 4 - Webhook Security

An intentionally forged signature returned HTTP 400 with the database
unchanged. The valid real event was replayed and returned HTTP 200 duplicate.
Exactly one stripe_events row remained and no duplicate subscription was
created.

## Architecture Flow

~~~text
Client
  ↓
API route
  ↓
Validation
  ↓
Quota check
  ↓
Usage service
  ↓
Usage event
  ↓
Monthly rollup / cost
~~~

~~~text
Stripe Checkout
  ↓
Stripe event
  ↓
Signature verification
  ↓
Event deduplication
  ↓
Subscription update
  ↓
Tenant plan
~~~

## Current Regression

~~~text
.\\venv\\Scripts\\python.exe -m pytest -q
70 passed
~~~

The suite completes with one existing Starlette/httpx deprecation warning.
The warning does not affect the result.
