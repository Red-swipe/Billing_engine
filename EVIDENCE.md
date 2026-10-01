# Section 6 Acceptance Evidence

This document is an examiner-facing acceptance checklist for the current
repository. The current full regression result is **85 passed**. Claims below
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
| Full regression | PASS | `85 passed` |

Tenant API keys are stored only as SHA-256 hashes. Isolation tests use different
tenant IDs and different API keys.

### Database Persistence / Alembic Migration Evidence

- Migration chain exists: **PASS** — `alembic/versions/20260930_0001_initial_schema.py`
  is the current baseline and `alembic heads` resolves to `20260930_0001`.
- Fresh database created through Alembic: **PASS** —
  `tests/test_migrations.py::test_fresh_database_is_created_by_alembic_and_has_required_schema`
  runs `alembic upgrade head` against an isolated empty SQLite database.
- Required tables and columns verified: **PASS** — the test inspects `plans`,
  `tenants`, `subscriptions`, `usage_events`, and `stripe_events`, including
  `usage_events.response_status_code` and `stripe_events.processed_at`.
- Required index verified: **PASS** — the test verifies
  `ix_usage_events_tenant_created_at` contains `(tenant_id, created_at)` in
  that order.
- Application compatibility: **PASS** —
  `tests/test_migrations.py::test_application_can_use_schema_created_by_alembic`
  creates a tenant through the API using the migrated database.
- Historical existing-schema upgrade: **NOT PROVEN** — the repository had no
  genuine pre-Alembic revision or migration snapshot. The baseline is honest
  about that limitation; existing databases from the prior `create_all`-only
  implementation may require explicit recreation or manual migration.
- Full regression after migration work: **PASS** - `85 passed`.

## Section 6 Checklist

| Requirement | Status | Evidence / Test | Observed result |
|---|---|---|---|
| 1. One usage event per action, deduped by key | PASS | test_generate_creates_exactly_one_usage_event; test_same_key_twice_returns_identical_response_and_one_row; test_concurrent_identical_keys_create_exactly_one_event | One successful action creates one usage_events row. Replays return the stored result and do not add a row. |
| 2. Quota checked before action | PASS | test_api_quota_boundary_at_limit; test_token_quota_boundary_at_limit; app/services/usage_service.py | Quota is checked before insert. Rejected requests create zero usage rows and leave the rejected key retryable. |
| 3. 429 / 402 clear error responses | PASS | `tests/test_stage_b.py::test_api_quota_boundary_at_limit`; inactive-subscription tests; `app/routes/generate.py` | 429 and 402 retain distinct structured errors; quota 429 now includes a non-negative integer `Retry-After` until the UTC month reset. |
| 4. Monthly usage rolls into cost | PASS | `tests/test_stage_d.py::test_usage_cost_includes_api_calls_and_uses_month_window`; `GET /usage/{tenant_id}` | Monthly API calls and token buckets are aggregated in the UTC half-open window; cost is token cost plus configured API-call cost, with previous-month rows excluded. |
| 5. Token pricing rules | PASS | `tests/test_stage_d.py` pricing tests | Input is $0.00025/1k, cached input is $0.000025/1k, output is $0.00075/1k; reasoning is charged once at the output rate; results are deterministic integer cents. |
| 6. Pricing pinned in configuration | PASS | `app/config.py`; `tests/test_stage_d.py::test_pricing_rates_are_pinned_in_settings`; `test_configured_api_call_price_is_added_as_integer_cents` | Token rates and `API_CALL_PRICE_CENTS` are Settings fields loaded from environment-configurable values and consumed by the pricing function. |
| 7. Checkout works end-to-end in Stripe test mode | PASS | Recorded final real Stripe test-mode probe | Real Stripe Checkout completed successfully for Tenant 1 in test mode. |
| 8. Webhooks verify signatures | PASS | Real forged-signature probe; test_forged_signature_returns_400_and_writes_nothing; test_signature_from_wrong_secret_is_rejected | Forged signature returned HTTP 400 and changed no database state. |
| 9. Webhooks deduplicate events | PASS | Real replay probe; test_replay_same_event_twice_processes_once; test_duplicate_delivery_does_not_reapply_business_operation | Replays returned HTTP 200 duplicate. Exactly one stripe_events row existed and no duplicate subscription was created. |
| 10. Webhooks update the subscription/plan | PASS | Real checkout.session.completed probe; test_checkout_session_completed_upgrades_to_pro; test_subscription_updated_syncs_state; test_subscription_deleted_marks_inactive_and_blocks_generate | The final real completed-checkout event changed Tenant 1 to Pro with an active subscription. customer.subscription.updated was not observed during the final real checkout probe; lifecycle behavior is covered by automated tests. |
| 11. Real persistence | PASS | SQLite models/database; fresh-clone evidence; real Stripe probe | Plans, tenants, usage events, Stripe events, customers, and subscriptions persist in SQLite across requests. |
| 12. Tenant data isolation | PASS | `tests/test_auth.py`; `app/auth.py`; protected generate/usage/checkout routes | Tenant API keys are hashed, authenticated tenants are authoritative, and cross-tenant body/URL access is rejected. |
| 13. Required database/index behavior | PASS | app/models.py; test_concurrent_identical_keys_create_exactly_one_event; SQLite schema evidence | Primary keys, foreign keys, unique email/customer/event/idempotency constraints, and indexes for tenant/event timestamps are present. |
| 14. Layered architecture | PASS | app/routes, app/services, app/models.py, app/database.py | FastAPI routes handle transport/validation, services handle metering/pricing/Stripe logic, and SQLAlchemy models/database handle persistence. |
| 15. Validation / clean 4xx responses | PASS | test_invalid_payloads_rejected_without_creating_events; test_unknown_tenant_is_404; checkout/webhook negative-path tests | Invalid payloads are rejected without usage rows; unknown tenants return 404; inactive subscriptions return 402; quota exhaustion returns 429; invalid webhook signatures return 400. |
| 16. Idempotency | PASS | Metering replay tests and Stripe replay tests listed above | Client-supplied X-Idempotency-Key makes metering replay the original status/body; Stripe event IDs make webhook processing exactly-once. |
| 17. Secrets hygiene | PASS | app/config.py; checkout secret-response test; repository history/status checks | Stripe credentials are loaded from environment variables, the secret key is not returned by checkout, and runtime secret/database/log files were not staged in the checkpoint commits. |
| 18. Background job with retries/failure alert | PASS | `tests/test_background_jobs.py`; `app/services/background_jobs.py` | Usage-cost reconciliation is scheduled with FastAPI BackgroundTasks, retries are bounded/configurable, and one structured alert is emitted only after final failure. |
| 19. Schema migrations | PASS with historical limitation | `alembic.ini`; `alembic/env.py`; `alembic/versions/20260930_0001_initial_schema.py`; `tests/test_migrations.py` | Fresh schema and application compatibility are verified through Alembic. No genuine pre-Alembic revision exists, so an upgrade from the old `create_all`-only schema is not claimed. |

### Item 7 — Cost Calculation / Pricing / Retry-After Evidence

- Pricing configuration: **PASS** — `app.config.Settings` owns the three
  token rates and `API_CALL_PRICE_CENTS`; `pricing.py` contains no hidden
  request-local pricing values.
- Token cost: **PASS** — exact `Decimal` arithmetic with half-up rounding
  returns integer cents, preserving the established rates and reasoning rule.
- API-call cost: **PASS** — `cost_cents` uses
  `token_cost_cents + api_calls_used * API_CALL_PRICE_CENTS`. The assignment
  does not specify a numeric API-call rate, so the documented default is zero
  cents and a non-zero value can be supplied through configuration.
- Monthly window and isolation: **PASS** — current-month calls/tokens are
  included, prior-month events are excluded, and existing tenant-authenticated
  usage isolation remains covered by `tests/test_auth.py`.
- Subscription price distinction: **PASS** — `plans.price_cents` and the
  Stripe Price ID describe subscription billing; they are not added to usage
  `cost_cents`.
- Retry guidance: **PASS** — quota HTTP 429 responses retain their structured
  error and include a non-negative integer `Retry-After` calculated as the
  seconds until the UTC calendar-month reset.

## Final Real Stripe Evidence - 2026-10-01

The October 1 Tenant 1 run is the authoritative final Stripe evidence.

Recorded from the real Stripe test-mode checkout and fresh Alembic database probe:

~~~text
Checkout Session: cs_test_a1gLIcqUiMcMe9HOs9jYWwT7IpN2YqtGpx367LKJH1Bdrw11Z1tLyXbCUQ
Payment completed: PASS (status=complete, payment_status=paid)
Tenant: 1
Real event ID: evt_1ULj2gCSRoP1ezLYzMAMUhTd
Event type: checkout.session.completed
Webhook response: HTTP 200
Stripe customer ID: cus_VMB4Gjsdp8Tqyq
Stripe subscription ID: sub_1ULj2fCSRoP1ezLYdwdug5UE
Plan: Pro
Subscription status: active
Fresh Alembic database: probe_final.db, revision 20260930_0001 (head)
Required index: ix_usage_events_tenant_created_at = (tenant_id, created_at)
~~~

### P3 live usage after upgrade

Captured from the final Stripe database (`probe_final.db`) through the
authenticated Tenant 1 endpoint:

~~~text
GET /usage/1
{"tenant_id":1,"month_start":"2026-10-01T00:00:00Z","month_end":"2026-11-01T00:00:00Z","plan":"Pro","api_calls_used":0,"api_calls_limit":50000,"tokens_used":0,"tokens_limit":5000000,"cost_cents":0}
~~~

This is the live Pro usage response after the real Stripe upgrade; it shows
`api_calls_limit=50000` and `tokens_limit=5000000`.

### P5 exact pricing totals

The same authenticated Tenant 1 was measured before and after one new generate
request with `input_tokens=100000`, `cached_input_tokens=200000`,
`output_tokens=300000`, and `reasoning_tokens=300000`:

~~~text
GET /usage/1 before
{"tenant_id":1,"month_start":"2026-10-01T00:00:00Z","month_end":"2026-11-01T00:00:00Z","plan":"Pro","api_calls_used":0,"api_calls_limit":50000,"tokens_used":0,"tokens_limit":5000000,"cost_cents":0}
POST /generate
{"cached_input_tokens": 200000, "completion": "dummy completion", "input_tokens": 100000, "output_tokens": 300000, "reasoning_tokens": 300000, "tenant_id": 1, "usage_event_id": 1}
GET /usage/1 after
{"tenant_id":1,"month_start":"2026-10-01T00:00:00Z","month_end":"2026-11-01T00:00:00Z","plan":"Pro","api_calls_used":1,"api_calls_limit":50000,"tokens_used":900000,"tokens_limit":5000000,"cost_cents":48}
~~~

Arithmetic: `100000 + 200000 + 300000 + 300000 = 900000` tokens.
Cost: `2.5` input cents + `0.5` cached-input cents + `22.5` output cents +
`22.5` reasoning cents = `48` cents. Reasoning tokens intentionally use the
output-token rate of 75 cents per million in this pricing model. The observed usage delta is
`tokens_used +900000` and `cost_cents +48`, matching the expected result.

The real event payload was retrieved from Stripe test mode and delivered to
POST /webhooks/stripe with a valid Stripe signature. customer.subscription.created
was observed in Stripe for this flow; customer.subscription.updated was not
observed and remains covered by automated tests only.

### Forged Signature

~~~text
Forged signature response: HTTP 400
Forged response body: {"detail":"Invalid webhook signature"}
Database unchanged: PASS
~~~

The exact stored payload for the real event was sent with an intentionally
invalid signature. The event count and Tenant 1 subscription/plan state were
unchanged.

### Replay / Deduplication

~~~text
Original live delivery: HTTP 200 processed
Replay: HTTP 200 duplicate
Exactly one stripe_events row: PASS
Duplicate subscription: NO
Stored row: evt_1ULj2gCSRoP1ezLYzMAMUhTd | checkout.session.completed | processed=1
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
after successful payment and after the webhook had upgraded Tenant 1 to Pro; it
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

### Probe 3 - Earlier real Stripe probe (Tenant 2)

The earlier September 30 probe used Free Tenant 2 for real Stripe test-mode
Checkout, completed payment, received
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
85 passed
~~~

The suite completes with one existing Starlette/httpx deprecation warning.
The warning does not affect the result.
