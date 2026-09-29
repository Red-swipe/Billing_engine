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
| `idempotency_key` | str | NOT NULL, UNIQUE | Client-supplied via `X-Idempotency-Key`; the retry guard |
| `response_body` | text | NULLABLE | JSON string snapshot of the response, written in the same transaction |
| `response_status_code` | int | NULLABLE | Status to replay for this key. Required so a retry returns the original status, not just the original body |
| `created_at` | datetime | NOT NULL, INDEX | Naive UTC |

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
| `stripe_event_id` | str | NOT NULL, UNIQUE | Stripe's `evt_...`; the replay guard |
| `event_type` | str | NOT NULL | e.g. `checkout.session.completed` |
| `payload` | text | NOT NULL | Raw JSON body as received |
| `processed` | bool | NOT NULL, DEFAULT false | Set true after handlers succeed |
| `processed_at` | datetime | NULLABLE | Set when a supported event is applied |
| `created_at` | datetime | NOT NULL | |

Stripe retries any webhook it does not receive a `2xx` for, so every event is
written before handling. A duplicate delivery hits `UNIQUE(stripe_event_id)`
and is acknowledged without reprocessing.

## Plan Limits

| Plan | api_calls / month | tokens / month | price (cents) |
|---|---|---|---|
| Free | 1,000 | 100,000 | 0 |
| Pro | 50,000 | 5,000,000 | 2,000 |

`tokens` is the sum of `input_tokens + cached_input_tokens + output_tokens +
reasoning_tokens` over the current period. `cached_input_tokens` **counts toward
the limit** — cached tokens are free to the provider but still consume the
tenant's quota, otherwise cache-heavy tenants would meter near zero.

The boundary is **inclusive at the limit**: a tenant sitting at exactly
`api_calls_limit` (1,000 on Free) is allowed its last call, and the next call is
rejected. The check is `used + requested > limit`, not `>=`.

## Implementation Status

| Area | State |
|---|---|
| `POST /tenants` | Implemented (incl. automatic active Free subscription) |
| `POST /generate` | Implemented (header idempotency, quota before insert, exact replay) |
| `GET /usage/{tenant_id}` | Implemented (UTC calendar month) |
| `GET /checkout/{tenant_id}` | Implemented; Stripe-configured checkout session creation. |
| `POST /webhooks/stripe` | Implemented; raw-body verification and deduplicated sync. |
| Pricing / cost calculation | **Not implemented.** `price_cents` exists on `plans`; no cost is computed or stored per request. |
| `stripe_events` table | Implemented unique event claim and processed marker. |
| Subscription lifecycle webhooks | Implemented for updated/deleted events. |
| Migrations | Alembic is a dependency but unused. Tables are created with `Base.metadata.create_all`. |

## API Surface

### `POST /tenants`

Create a tenant on the Free plan.

Request: `{ "name": str, "email": str }`
Response `201`: `{ "id": int, "name": str, "email": str, "plan": "Free", "status": "active" }`

Behavior:

1. Look up `email`. An existing tenant returns `409` (`UNIQUE` constraint on
   `tenants.email`).
2. Insert the tenant with `plan_id` = Free and `status` = `'active'`.
3. In the **same transaction**, insert the tenant's `subscriptions` row with
   `plan_id` = Free and `status` = `'active'`, and `stripe_subscription_id` left
   NULL (a Free tenant has no Stripe subscription).
4. Commit once. A tenant is never observable without its subscription, so
   `POST /generate` never finds a just-created tenant in the `402` state.

A new tenant is on the Free plan by default, so this endpoint must return a
tenant that can immediately generate. A Free tenant is not a Stripe customer
and has no billing relationship to wait on, which is why the subscription is
active at creation rather than awaiting a webhook.

Errors: `409` if `email` already exists (the `UNIQUE` constraint).

### `POST /generate`

A **dummy billable action**. There is no real model call. The client supplies
the token counts, and the engine's job is to meter them exactly once, enforce
quota, and support exact replay. This keeps the billing machinery real while
removing any dependency on an inference provider.

#### Idempotency key

Supplied **only** via the `X-Idempotency-Key` request header. It is not part of
the JSON body.

- A request with no `X-Idempotency-Key` (or a blank one) is rejected with
  **400** and creates nothing.
- The server never synthesises a key. A server-generated key cannot make a
  retry idempotent, which is the entire purpose of the header.

#### Request

```json
{
  "tenant_id": 1,
  "input_tokens": 100,
  "cached_input_tokens": 0,
  "output_tokens": 50,
  "reasoning_tokens": 20
}
```

All four token counts must be non-negative integers. Pydantic rejects
negatives, non-integers, and malformed `tenant_id` with **422** before any
database work, so a validation failure can never create a `usage_events` row.

#### Response (200)

Deterministic for a given usage row:

```json
{
  "tenant_id": 1,
  "completion": "dummy completion",
  "usage_event_id": 123,
  "input_tokens": 100,
  "cached_input_tokens": 0,
  "output_tokens": 50,
  "reasoning_tokens": 20
}
```

`usage_event_id` is included so a caller can reconcile against the row.

The response is serialised with `json.dumps(..., sort_keys=True)` on **both** the
first delivery and every replay, using the same strategy as the stored
`response_body`. The bytes the client first receives are therefore identical to
the bytes a retry receives, and identical to what is persisted on the row. Key
order is sorted for this reason; it carries no meaning in the contract.

#### Authoritative order of operations

Quota is checked **before** the insert. A rejected request therefore never
leaves a row behind.

1. Validate the request (`400` / `422`). No writes.
2. Look up the idempotency key. If a committed row exists, replay it (below).
3. Check the subscription. Not active -> **402**. No writes.
4. Check the tenant's monthly quota. Over limit -> **429**. No writes.
5. Insert exactly one `usage_events` row carrying the key.
6. Set `response_body` and `response_status_code` on that row, then `commit()`
   once.
7. Return the stored body with the stored status.

An earlier draft of this document specified insert-then-check-quota. That
ordering is wrong for two reasons and has been removed: a 429 would leave an
orphan row that counted against the very limit it just exceeded, and the
rejected attempt would permanently consume its idempotency key.

#### Rejected requests do not consume their key

Because a 402 or 429 writes no row, the key is never recorded, and the rejected
attempt consumes **neither quota nor the key**. A client that was rate limited
may retry the **same** key later and succeed once eligible:

- first attempt -> `429`, no row written, no quota consumed
- tenant becomes eligible again
- same key retried -> `200`, exactly one row

There is no "failed usage event" row. Usage events mean "this was billed".

`402` and `429` are deliberately distinct. A lapsed subscription is a billing
state problem, not quota exhaustion, and the two are never conflated.

#### Exact replay

A repeat of a previously successful key returns the **stored** status code and
the **stored** body — byte-identical JSON, including the original
`usage_event_id`. No new row is created and no quota is consumed. This is why
`response_status_code` exists alongside `response_body`: storing the body alone
cannot reproduce the status.

One row per successful billable request. A key that produced a `402` or `429`
produced no row, so there is no "failed event" to replay.

#### Concurrency

Two distinct problems, two distinct mechanisms.

**Double-metering a retried key.** The `UNIQUE` constraint on
`usage_events.idempotency_key` is the authority. The pre-insert lookup in step 2
is only a fast path that avoids raising an exception on the common sequential
retry; correctness does not depend on it, because the `INSERT` still runs and
still hits the constraint. An `IntegrityError` on that constraint is caught, the
session is rolled back, and the winning request's committed row is read back and
replayed. Both callers end up with the same response.

**Quota overshoot.** A lookup-then-insert is a read-modify-write race, so steps
4-6 are held under a process-wide lock. This is sufficient for the current
architecture: one uvicorn process against SQLite, which serialises writers
anyway. A multi-process or multi-worker deployment would need a real database
lock (`SELECT ... FOR UPDATE`, or `BEGIN IMMEDIATE` on SQLite) instead of a
Python lock. That is out of scope here and is called out as a known limit.

### `GET /usage/{tenant_id}`

Current **UTC calendar-month** usage against the tenant's plan limits. No
pricing or cost — that is a later stage.

The window is half-open: `[month_start, month_end)`, where `month_start` is
`00:00:00` UTC on the 1st and `month_end` is `00:00:00` UTC on the 1st of the
next month. An event stamped exactly at `month_start` counts; one stamped
exactly at `month_end` does not. Server local time is never used.

`tokens_used` is `input_tokens + cached_input_tokens + output_tokens +
reasoning_tokens` summed over the window. A tenant with no events returns zero.

```json
{
  "tenant_id": 1,
  "month_start": "2026-01-01T00:00:00Z",
  "month_end": "2026-02-01T00:00:00Z",
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

The endpoint is intentionally not an upgrade operation. It returns `404` for
an unknown tenant, `503` when `STRIPE_SECRET_KEY` or `STRIPE_PRO_PRICE_ID` is
missing, and `409` when the tenant already has an active Pro subscription.
The first checkout creates a Stripe Customer and stores its ID on
`tenants.stripe_customer_id`; subsequent requests reuse that ID. Session
metadata and `client_reference_id` both contain the application tenant ID.

### Stripe tenant mapping and webhook synchronization

`checkout.session.completed` resolves the tenant from the application-written
`metadata.tenant_id`, with `client_reference_id` and the persisted Stripe
customer ID as controlled fallbacks. Subscription lifecycle events resolve via
the persisted Stripe subscription ID first, then customer ID. Unresolvable
events are acknowledged and recorded as unprocessed; no tenant is guessed.

The webhook reads the raw request bytes and verifies them with Stripe's official
signature helper before parsing or writing. It claims `stripe_events` using the
unique `stripe_event_id` constraint before applying a handler, then commits the
event row and local subscription change together. Invalid signatures return
`400` and write nothing. A verified duplicate returns `200` with `result=duplicate`.

Supported transitions are:

- `checkout.session.completed`: Free -> Pro/active, storing customer and
  subscription IDs.
- `customer.subscription.updated`: synchronizes the local plan/status and
  period boundaries using the known Stripe identifiers.
- `customer.subscription.deleted`: keeps history and marks the local
  subscription `canceled`, so `/generate` returns the existing Stage 2 `402`.

Unknown valid event types are recorded as ignored without changing billing
state. Stripe credentials are never returned by the API, and no local pricing
engine is introduced.

### `POST /webhooks/stripe`

Raw-body Stripe webhook receiver. Signature verified against
`STRIPE_WEBHOOK_SECRET` before any parsing of the payload.

Behavior:

1. Verify the `Stripe-Signature` header. Invalid signature returns `400` and
   nothing is written.
2. Insert into `stripe_events` (`stripe_event_id`, `event_type`, `payload`).
   On `UNIQUE(stripe_event_id)` conflict, return `200` immediately — this is a
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
