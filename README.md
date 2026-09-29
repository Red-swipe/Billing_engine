# Billing Engine

A multi-tenant usage metering and billing engine for an AI SaaS product. It
records one usage event per billable request, enforces per-plan monthly quota,
supports exactly-once accounting through client-supplied idempotency keys, and
reports monthly usage rollups.

This repository is at **Stage 2 (core billing logic)**. Stripe checkout and
webhooks are designed but **not implemented** — see [Current Status](#current-status).

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

## Architecture

```
app/
  main.py              FastAPI app; create_all on startup; router registration
  config.py            Settings loaded from .env via python-dotenv
  database.py          Declarative Base, engine, SessionLocal, get_db dependency
  models.py            SQLAlchemy models: Plan, Tenant, Subscription,
                       UsageEvent, StripeEvent
  routes/
    tenants.py         POST /tenants
    generate.py        POST /generate
    usage.py           GET /usage/{tenant_id}
  services/
    quota.py           UTC calendar-month window, aggregation, limit checks
    usage_service.py   The only writer of usage_events; locking and replay
seed.py                Idempotent database initialization
tests/                 pytest suite
```

Two design points worth knowing before reading the code:

**One row per request, not one row per token type.** A single request that spends
input tokens on two upstream calls cannot be represented by a
`type` + `quantity` ledger — the second write has nowhere to live. It also
fragments idempotency across N rows, so a partially-applied retry becomes
double-billing. Collapsing to one row makes the idempotency key a hard
one-row-one-key guarantee from a single `UNIQUE` constraint. The full reasoning
is in [DESIGN.md](DESIGN.md).

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
```

### Configure

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

`.env.example` ships with placeholder values that are safe to use for local
development. For the current stage no Stripe value is required — Stripe code is
not implemented yet.

## Database initialization

Tables are created by SQLAlchemy at application startup (`create_all` in the
FastAPI lifespan). Plans are **not** created automatically, so seed once before
first use:

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

## API endpoints

### `POST /tenants`

Creates a tenant on the Free plan, together with an active Free subscription in
the same transaction, so the returned tenant can generate immediately.

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
| `429` | Would exceed a plan limit | No |

A repeated key returns the stored status and body byte-for-byte, including the
original `usage_event_id`. Because `402` and `429` write no row, their keys are
never consumed and can be retried later.

### `GET /usage/{tenant_id}`

Current UTC calendar-month usage against plan limits. No pricing — that is a
later stage.

```bash
curl http://127.0.0.1:8000/usage/2
```

```json
{"tenant_id": 2, "month_start": "2026-09-01T00:00:00Z", "month_end": "2026-10-01T00:00:00Z", "plan": "Free", "api_calls_used": 3, "api_calls_limit": 1000, "tokens_used": 510, "tokens_limit": 100000}
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

## Testing

```bash
pytest -q
```

```
27 passed
```

The suite runs against a throwaway SQLite database per test, created under the
system temp directory, so it never touches your development database. It covers
normal metering, idempotent replay (including byte-level equality and
8-thread concurrency), missing keys, exact quota boundaries for both calls and
tokens, rejected-key retryability, lapsed subscriptions, monthly rollup, and
previous-month exclusion.

Verified results are recorded in [EVIDENCE.md](EVIDENCE.md).

## Stripe (not yet implemented)

`STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` are present in `.env.example`
as placeholders because the Stage 3 design is settled and documented in
[DESIGN.md](DESIGN.md), but **no Stripe code exists in this repository yet**.
There is no checkout session creation, no webhook receiver, and no signature
verification. The `stripe_events` table exists in the schema with a `UNIQUE`
constraint on `stripe_event_id`, but nothing writes to it.

When Stripe is implemented, setup will require:

1. Real values for `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in `.env`
2. A `STRIPE_WEBHOOK_SECRET` obtained from `stripe listen` or the dashboard
3. `APP_BASE_URL` matching the address the app is served from, used for Checkout
   redirect URLs

## Current Status

| Area | State |
|---|---|
| Tenant creation, automatic Free subscription | Implemented |
| `POST /generate` metering, idempotency, quota | Implemented |
| `GET /usage/{tenant_id}` monthly rollup | Implemented |
| Test suite (27 tests) | Implemented |
| `GET /checkout/{tenant_id}` | **Not implemented** — design only |
| `POST /webhooks/stripe` | **Not implemented** — design only |
| Pricing / cost calculation | **Not implemented** |
| `stripe_events` table | Schema only, never written |
| Alembic migrations | Dependency present, unused; tables created via `create_all` |

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
