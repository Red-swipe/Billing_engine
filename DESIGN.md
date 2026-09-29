# Billing Engine — Design

## Problem Statement

AI SaaS applications meter two resources per request: **API calls** and **tokens**.
Token usage is fragmented across `input`, `cached_input`, `output`, and `reasoning`
buckets, each priced differently by the model provider.

The problem: a tenant must be able to send a request, and the system must record
exactly what that request cost in metered units — once, and only once — even if
the client retries. Downstream, that metered usage must be summed against a plan
limit, and upgrades must flow through Stripe Checkout.

Two constraints shape the whole design:

1. **One request, one row.** Usage is recorded at request granularity so retry
   safety and per-request attribution are trivially correct.
2. **All money is integer cents.** No floats, no `Decimal` drift in stored
   amounts. Any money value persisted or passed across a boundary is an integer
   count of cents (e.g. `2000` = $20.00). Stripe amounts are cents natively, so
   this mapping is lossless in both directions.

## Schema

All primary keys are integers. All timestamps are stored in UTC.

### `tenants`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | int | PK, autoincrement | |
| `name` | str | NOT NULL | Human-readable tenant label |
| `email` | str | NOT NULL, UNIQUE | Billing contact |
| `stripe_customer_id` | str | NULLABLE, UNIQUE | Set after Checkout completes |
| `plan_id` | int | FK -> `plans.id`, NOT NULL, DEFAULT 1 | Free plan on create |
| `status` | str | NOT NULL, DEFAULT `'active'` | `active` \| `canceled` |
| `created_at` | datetime | NOT NULL | |

### `plans`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | int | PK, autoincrement | |
| `name` | str | NOT NULL, UNIQUE | `Free`, `Pro` |
| `api_calls_limit` | int | NOT NULL | Per month |
| `tokens_limit` | int | NOT NULL | Per month, summed across all token buckets |
| `price_cents` | int | NOT NULL, DEFAULT 0 | **Integer cents.** 0 for Free |
| `created_at` | datetime | NOT NULL | |

Seed rows:

| id | name | api_calls_limit | tokens_limit | price_cents |
|---|---|---|---|---|
| 1 | Free | 1,000 | 100,000 | 0 |
| 2 | Pro | 50,000 | 5,000,000 | 2000 |

### `subscriptions`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | int | PK, autoincrement | |
| `tenant_id` | int | FK -> `tenants.id`, NOT NULL, UNIQUE | One active subscription per tenant |
| `plan_id` | int | FK -> `plans.id`, NOT NULL | |
| `stripe_subscription_id` | str | NULLABLE, UNIQUE | Populated by webhook |
| `status` | str | NOT NULL, DEFAULT `'inactive'` | `inactive` \| `active` \| `canceled` |
| `current_period_start` | datetime | NULLABLE | |
| `current_period_end` | datetime | NULLABLE | |
| `created_at` | datetime | NOT NULL | |

### `usage_events`

**One row per request.** This is the core metering table.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | int | PK, autoincrement | |
| `tenant_id` | int | FK -> `tenants.id`, NOT NULL, INDEX | |
| `api_calls` | int | NOT NULL, DEFAULT 1 | Always 1 today; column so a future batch row can carry N |
| `input_tokens` | int | NOT NULL, DEFAULT 0 | |
| `cached_input_tokens` | int | NOT NULL, DEFAULT 0 | |
| `output_tokens` | int | NOT NULL, DEFAULT 0 | |
| `reasoning_tokens` | int | NOT NULL, DEFAULT 0 | |
| `idempotency_key` | str | NOT NULL, UNIQUE | Client-supplied; the retry guard |
| `response_body` | text | NULLABLE | JSON string snapshot of the response |
| `created_at` | datetime | NOT NULL, INDEX | |

**Why one row per request instead of a `type` + `quantity` design.**

The obvious alternative is a generic ledger: one row per *meter type* with a
`type` discriminator (`input_tokens`, `output_tokens`, ...) and a `quantity`
integer. That design breaks in three specific ways:

1. **It cannot express the same type twice in one request.** A request may spend
   input tokens across two upstream calls. A type+quantity ledger has no
   dimension to hold both; the second write either duplicates the type or
   requires an artificial sub-type.
2. **It fragments idempotency across N rows.** A single request generating four
   token writes needs four separate idempotency checks, and a retry that
   partially succeeds leaves the ledger in a state no single uniqueness
   constraint can protect. Partial application is the failure mode that produces
   double-billed tenants.
3. **Token buckets are known and closed.** The set is exactly `input`,
   `cached_input`, `output`, `reasoning`. A discriminator column buys generality
   the system will never use, at the cost of a non-null `type` string on the
   hottest write path in the database.

The one-row-per-request design collapses all three problems:

- **One idempotency key maps to exactly one row.** The `UNIQUE` constraint on
  `idempotency_key` is a hard database-level guarantee. A retried request
  attempts an insert, hits the constraint, and the caller returns the previously
  stored `response_body`. Metering is all-or-nothing because it is one insert.
- **All buckets are real, typed, `NOT NULL` integer columns.** Summation is
  `SUM(input_tokens + cached_input_tokens + output_tokens + reasoning_tokens)`
  with no type dispatch, and no NULL handling.
- **The row is the natural audit record.** It carries the request's own
  `response_body` and `created_at`, so a disputed meter can be traced back to the
  exact call without joining another table.

`api_calls` defaults to 1 rather than being removed, so a future batch or
multi-step request can write a single row with `api_calls = N` instead of
requiring a schema change.

### `stripe_events`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | int | PK, autoincrement | |
| `event_id` | str | NOT NULL, UNIQUE | Stripe's `evt_...`; the replay guard |
| `event_type` | str | NOT NULL | e.g. `checkout.session.completed` |
| `payload` | text | NOT NULL | Raw JSON body as received |
| `processed` | bool | NOT NULL, DEFAULT false | Set true after handlers succeed |
| `created_at` | datetime | NOT NULL | |

Stripe retries any webhook it does not receive a `2xx` for, so every event is
written before handling. A duplicate delivery hits `UNIQUE(event_id)` and is
acknowledged without reprocessing.

## Plan Limits

| Plan | api_calls / month | tokens / month | price (cents) |
|---|---|---|---|
| Free | 1,000 | 100,000 | 0 |
| Pro | 50,000 | 5,000,000 | 2,000 |

`tokens` is the sum of `input_tokens + cached_input_tokens + output_tokens +
reasoning_tokens` over the current period. `cached_input_tokens` **counts toward
the limit** — cached tokens are free to the provider but still consume the
tenant's quota, otherwise cache-heavy tenants would meter near zero.

## API Surface

### `POST /tenants`

Create a tenant on the Free plan.

Request: `{ "name": str, "email": str }`
Response `201`: `{ "id": int, "name": str, "email": str, "plan": "Free", "status": "active" }`

Errors: `409` if `email` already exists (the `UNIQUE` constraint).

### `POST /generate`

Metered inference call. This is the only endpoint that writes `usage_events`.

Request:

```json
{
  "tenant_id": 1,
  "idempotency_key": "client-generated-uuid",
  "prompt": "...",
  "model": "...",
  "stream": false
}
```

Behavior:

1. Insert a `usage_events` row carrying the `idempotency_key`.
   If the insert violates `UNIQUE(idempotency_key)`, the row already exists —
   return the stored `response_body` and **do not** bill again.
2. Enforce the tenant's plan limits. A request that would exceed
   `api_calls_limit` or `tokens_limit` for the period returns `429` and writes no
   `usage_events` row.
3. Return the completion, persisting it into that same row's `response_body`.

Response: completion payload plus
`{ "usage_event_id": int, "input_tokens": int, "output_tokens": int,
"cached_input_tokens": int, "reasoning_tokens": int }`

### `GET /usage/{tenant_id}`

Current-period usage rollup against plan limits.

Response:

```json
{
  "tenant_id": 1,
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-02-01T00:00:00Z",
  "plan": "Free",
  "api_calls_used": 812,
  "api_calls_limit": 1000,
  "tokens_used": 41230,
  "tokens_limit": 100000
}
```

### `GET /checkout/{tenant_id}`

Create a Stripe Checkout Session for the tenant's pending plan change and
return the redirect URL.

Response:

```json
{
  "tenant_id": 1,
  "plan": "Pro",
  "price_cents": 2000,
  "checkout_url": "https://checkout.stripe.com/..."
}
```

The session is created in `mode=subscription` with
`success_url={APP_BASE_URL}/` and
`cancel_url={APP_BASE_URL}/?checkout=cancelled`.

### `POST /webhooks/stripe`

Raw-body Stripe webhook receiver. Signature verified against
`STRIPE_WEBHOOK_SECRET` before any parsing of the payload.

Behavior:

1. Verify the `Stripe-Signature` header. Invalid signature returns `400` and
   nothing is written.
2. Insert into `stripe_events` (`event_id`, `event_type`, `payload`).
   On `UNIQUE(event_id)` conflict, return `200` immediately — this is a
   duplicate delivery.
3. Handle `checkout.session.completed`: set `tenants.stripe_customer_id`, create
   or update the `subscriptions` row, and move the tenant onto the purchased
   plan.
4. Mark `processed = true` and return `200`.

## Non-Goal

**No refunds, no invoices, no proration.** This engine records usage and starts
subscriptions. It never issues a refund, never generates or stores an invoice,
and never computes proration for mid-cycle plan changes. A plan change takes
effect at the next period boundary via Stripe's own subscription behavior. Any
request for a partial refund, a credit note, or a proration line item is out of
scope and should be rejected rather than approximated.
