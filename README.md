# Billing Engine

A multi-tenant usage metering and billing engine for an AI SaaS product. It
records one usage event per billable request, enforces per-plan monthly quota,
supports exactly-once accounting through client-supplied idempotency keys, and
reports monthly usage rollups.

After a fresh successful `POST /generate`, FastAPI schedules a read-only usage
cost reconciliation job after the response path. It retries transient failures
up to `BACKGROUND_JOB_MAX_ATTEMPTS` (default `3`). A permanently failing job
stops after the final attempt and emits one structured `ERROR` log containing
the job name, job identifier, attempt count, and failure reason. This is an
in-process background task, not production-grade distributed queue
infrastructure.

This repository is complete through **Items 1-7**.
Monthly usage cost is calculated deterministically from usage events and
returned as integer cents.

## What it does

Every billable action a tenant performs is metered. For each request the engine
records exactly one `usage_events` row containing the API call count and all
four token buckets (`input`, `cached_input`, `output`, `reasoning`). Monthly
quota is checked against the tenant's plan before anything is written, so a
rejected request never leaves a row behind and never consumes the client's
idempotency key.

`POST /generate` is a **dummy billable action**: it is a real metering path with
no real model behind it. The client supplies the token counts. This keeps the
billing machinery genuinely exercised without depending on an inference
provider.

Tenant-scoped endpoints require `X-API-Key`. Each tenant has a unique
SHA-256 API-key hash in the database; the authenticated tenant is the source of
truth rather than a request body or URL ID. Missing/invalid keys return `401`,
and cross-tenant access returns `403`.

## Architecture

```
app/
  main.py              FastAPI app; Alembic-owned startup; router registration
  config.py            Settings loaded from .env via python-dotenv
  database.py          Declarative Base, engine, SessionLocal, get_db dependency
  models.py            SQLAlchemy models: Plan, Tenant, Subscription,
                       UsageEvent, StripeEvent
  routes/
    tenants.py         POST /tenants
    generate.py        POST /generate
    usage.py           GET /usage/{tenant_id}
    checkout.py        POST /checkout/{tenant_id} (GET compatibility alias)
    webhooks.py        POST /webhooks/stripe
  services/
    quota.py           UTC calendar-month window, aggregation, limit checks
    usage_service.py   The only writer of usage_events; locking and replay
    stripe_service.py  Checkout, signature verification, and webhook sync
  main.py              GET / and /health status endpoints
seed.py                Idempotent database initialization
tests/                 pytest suite
```

Two design points worth knowing before reading the code:

**One row per request, not one row per token type.** A single request that spends
input tokens on two upstream calls cannot be represented by a
`type` + `quantity` ledger — the second write has nowhere to live. It also
fragments idempotency across N rows, so a partially-applied retry becomes
double-billing. Collapsing to one row makes the idempotency key a hard
one-row-one-key guarantee per tenant from a `UNIQUE(tenant_id, idempotency_key)`
constraint. The full reasoning is in [DESIGN.md](DESIGN.md).

**Quota is checked before the insert, not after.** The reverse order would let a
rejected request create an orphan row that itself consumed quota, and would
permanently burn the client's retry key.

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/Red-swipe/Billing_engine.git
cd Billing_engine

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

### Configure

`.env.example` ships with safe placeholders. Stripe values are required only for
the checkout and webhook paths; the automated suite mocks Stripe API calls and
does not require real credentials.

Required Stage 3 environment values:

```text
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRO_PRICE_ID=
APP_BASE_URL=http://localhost:8000
```

## Database initialization

Alembic migrations are the schema source of truth. Create or upgrade the
database before starting the application:

```bash
alembic upgrade head
```

Plans and the development tenant are **not** created by migrations. Seed them
separately once before first use:

```bash
python seed.py
```

Expected output:

```
Seeded plans [Free (1), Pro (2)] and tenant Test Tenant <test@example.com>
```

`seed.py` is idempotent — running it repeatedly updates existing plans and leaves
row counts unchanged. It reads `DATABASE_URL` from `.env`, defaulting to
`sqlite:///./billing.db`.

## Running

```bash
uvicorn app.main:app --reload
```

The API is then at `http://127.0.0.1:8000`, with interactive docs at `/docs`.
`GET /` and `GET /health` return a small service-status JSON response; the root
route is also the Stripe Checkout success redirect target.

## API endpoints

### `POST /tenants`

Creates a tenant on the Free plan, together with an active Free subscription in
the same transaction, so the returned tenant can generate immediately.
The response includes the newly generated API key once; only its hash is stored.

```bash
curl -X POST http://127.0.0.1:8000/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme Corp", "email": "billing@acme.example.com"}'
```

```json
{"id": 2, "name": "Acme Corp", "email": "billing@acme.example.com", "plan": "Free", "status": "active"}
```

Returns `409` if the email already exists. Note that `email-validator` rejects
reserved TLDs such as `.test` and `.localhost`.

### `POST /generate`

Meters one billable request. Requires an `X-Idempotency-Key` header; the server
never generates one, because a synthesized key cannot make a retry idempotent.

```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <tenant-api-key>" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{"tenant_id": 2, "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50, "reasoning_tokens": 20}'
```

```json
{"cached_input_tokens": 0, "completion": "dummy completion", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20, "tenant_id": 2, "usage_event_id": 1}
```

| Status | Cause | Usage row written? |
|---|---|---|
| `200` | Metered successfully | Yes, exactly one |
| `200` (replay) | Key already used | No — original response replayed |
| `400` | Missing or blank `X-Idempotency-Key` | No |
| `402` | Subscription not active | No |
| `404` | Unknown `tenant_id` | No |
| `422` | Invalid body | No |
| `429` | Would exceed a plan limit; includes `Retry-After` seconds until the UTC month reset | No |

A repeated key returns the stored status and body byte-for-byte, including the
original `usage_event_id`. Because `402` and `429` write no row, their keys are
never consumed and can be retried later.

### `GET /usage/{tenant_id}`

Current UTC calendar-month usage against plan limits, including deterministic
cost derived from the four token buckets and configured API-call rate.

```bash
curl http://127.0.0.1:8000/usage/2 -H "X-API-Key: <tenant-api-key>"
```

```json
{"tenant_id": 2, "month_start": "2026-09-01T00:00:00Z", "month_end": "2026-10-01T00:00:00Z", "plan": "Free", "api_calls_used": 3, "api_calls_limit": 1000, "tokens_used": 10000, "tokens_limit": 100000, "cost_cents": 1}
```

The window is half-open `[month_start, month_end)`. Server local time is never
used.

### Plan limits

| Plan | API calls / month | Tokens / month | Price (integer cents) |
|---|---|---|---|
| Free | 1,000 | 100,000 | 0 |
| Pro | 50,000 | 5,000,000 | 2,000 |

The boundary is inclusive at the limit: a tenant at exactly 1,000 calls is
allowed its last call, and the next is rejected. `cached_input_tokens` counts
toward the token limit.

All money is integer cents. There is no float money anywhere in the codebase.

### Usage cost and pricing configuration

Token rates are pinned in `app.config.Settings` and can be overridden through
the environment: input `$0.00025 / 1k`, cached input `$0.000025 / 1k`, and
output `$0.00075 / 1k`. Reasoning tokens use the output rate. The final token
amount is rounded half-up to integer cents using `Decimal`.

`cost_cents` is calculated as:

```text
token_cost_cents + api_calls_used * API_CALL_PRICE_CENTS
```

The assignment does not define a numeric API-call rate, so
`API_CALL_PRICE_CENTS` defaults to `0` and is explicitly configurable rather
than inventing a charge. API calls are still counted in the monthly rollup and
are included automatically if a non-zero rate is configured.

The Pro plan's `price_cents` and Stripe Price ID represent subscription
billing; the monthly usage `cost_cents` is metered usage and does not add the
subscription fee a second time.

## Testing

```bash
pytest -q
```

```
88 passed
```

The suite runs against a throwaway SQLite database per test, created under the
system temp directory, so it never touches your development database. It covers
normal metering, idempotent replay (including byte-level equality and
8-thread concurrency), missing keys, exact quota boundaries for both calls and
tokens, rejected-key retryability, lapsed subscriptions, monthly rollup, and
previous-month exclusion.

Verified results are recorded in [EVIDENCE.md](EVIDENCE.md).

## Stripe integration

`POST /checkout/{tenant_id}` creates a subscription-mode Checkout Session for a
Free tenant using `STRIPE_PRO_PRICE_ID`. It creates one Stripe customer on the
first request, persists the customer ID, and reuses it on later requests.
Checkout requires the API key belonging to the URL tenant. The seeded
development tenant uses `test-tenant-api-key`; this is not a production secret.
`APP_BASE_URL` supplies the success and cancel redirect URLs. A tenant already
on active Pro receives `409` and no new session is created. Checkout creation
alone never upgrades the local plan; that happens after a verified webhook.

`POST /webhooks/stripe` verifies the raw request body with
`STRIPE_WEBHOOK_SECRET`. Supported events are `checkout.session.completed`,
`customer.subscription.updated`, and `customer.subscription.deleted`. The
database UNIQUE constraint on `stripe_events.stripe_event_id` makes verified
replays and concurrent duplicate deliveries safe.
The webhook does not require `X-API-Key`; Stripe signature verification remains
its independent authentication mechanism.

### Local Stripe test-mode workflow

1. Set the four values above in `.env`, using a test secret and a recurring Pro
   test Price ID from the same Stripe account.
2. Run `python seed.py`, then start the API with `uvicorn app.main:app --reload`.
3. If installed, run `stripe listen --forward-to localhost:8000/webhooks/stripe`.
4. Put the CLI's printed `whsec_...` into `STRIPE_WEBHOOK_SECRET` and restart the
   app. The CLI forwards signed events to the raw-body webhook endpoint.
5. Create a tenant, open `/checkout/{tenant_id}`, complete Checkout in test
   mode, and inspect `/usage/{tenant_id}` after `checkout.session.completed`.

The normal pytest suite uses deterministic mocked Stripe API calls and genuine
SDK-generated webhook signatures. A real Stripe probe requires local test-mode
credentials and the Stripe CLI.

## Current Status

| Area | State |
|---|---|
| Tenant creation, automatic Free subscription | Implemented |
| `POST /generate` metering, idempotency, quota | Implemented |
| `GET /usage/{tenant_id}` monthly rollup | Implemented |
| Test suite (Items 1-7) | 88 passed |
| `POST /checkout/{tenant_id}` | Implemented; API-key protected (GET compatibility alias) |
| `POST /webhooks/stripe` | Implemented |
| Background usage-cost reconciliation | Implemented with bounded retries and failure alert |
| Pricing / monthly cost calculation | Implemented (`cost_cents`) |
| `stripe_events` table and UNIQUE replay guard | Implemented |
| Alembic migrations | Implemented; schema created and evolved through `alembic upgrade head` |

### Tenant API-key authentication

`POST /generate`, `GET /usage/{tenant_id}`, and `POST /checkout/{tenant_id}`
require `X-API-Key`. Cross-tenant body and URL IDs are rejected. The Stripe
webhook is intentionally excluded and remains signature-authenticated. This
repository includes a baseline Alembic migration for the current schema. A
database created by the earlier `create_all`-only version should be backed up
and migrated or recreated explicitly; no historical Alembic revision exists
for an automated upgrade from that pre-Alembic schema.

### Known limitation

Quota is protected by a process-wide lock in `usage_service.py`. That is
sufficient for the single-process uvicorn deployment this project targets, where
SQLite already serialises writers. A multi-worker deployment would need a real
database lock (`SELECT ... FOR UPDATE`, or `BEGIN IMMEDIATE` on SQLite) instead
of a Python lock.

## Project documents

| File | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | Problem statement, schema rationale, API contract, ordering guarantees, concurrency strategy |
| [EVIDENCE.md](EVIDENCE.md) | Raw probe output and test results for Stage 2 |
| [BUILDLOG.md](BUILDLOG.md) | Development progression by commit |
| [capstone.yaml](capstone.yaml) | Machine-readable project status |
