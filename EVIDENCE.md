# Stage 2 Evidence — Core Billing Logic

All values below are raw output from the Stage B verification runs. Nothing is
paraphrased or invented. Where a probe used direct database setup to establish a
pre-boundary state, that is stated explicitly.

Environment: Windows, Python 3.11.15, `venv/`, SQLite. Server under test:
uvicorn on `127.0.0.1:8001`, verified with `GET /health` → `200 {"status":"ok"}`.

---

## Probe 1 — Normal generation

Create a tenant on the Free plan:

```
POST /tenants
{"name": "Final Tenant", "email": "final@stageb.example.com"}

HTTP/1.1 201 Created
{"id":2,"name":"Final Tenant","email":"final@stageb.example.com","plan":"Free","status":"active"}
```

The tenant is returned with an active Free subscription created in the same
transaction, so it can generate immediately.

Send a metered request:

```
POST /generate
X-Idempotency-Key: final-stageb-key-001
Content-Type: application/json

{
  "tenant_id": 2,
  "input_tokens": 100,
  "cached_input_tokens": 0,
  "output_tokens": 50,
  "reasoning_tokens": 20
}

HTTP/1.1 200 OK
{"cached_input_tokens": 0, "completion": "dummy completion", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20, "tenant_id": 2, "usage_event_id": 1}
```

Observed values:

| Field | Value |
|---|---|
| `tenant_id` | 2 |
| `usage_event_id` | 1 |
| `input_tokens` | 100 |
| `cached_input_tokens` | 0 |
| `output_tokens` | 50 |
| `reasoning_tokens` | 20 |

Usage row count **0 → 1**.

Persisted state of that row, read directly from SQLite:

```
stored response_status_code: 200
stored response_body: {"cached_input_tokens": 0, "completion": "dummy completion", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20, "tenant_id": 2, "usage_event_id": 1}
```

The row, the status code and the body are written in a single transaction, which
is what makes faithful replay possible.

---

## Probe 2 — Idempotency

The identical request was sent again with the identical key
`X-Idempotency-Key: final-stageb-key-001`.

```
HTTP/1.1 200 OK
{"cached_input_tokens": 0, "completion": "dummy completion", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20, "tenant_id": 2, "usage_event_id": 1}
```

Byte comparison of the first response and the replay:

```
len1=163 sha1=D9CB3BD5E84A8D80E44C2BA1B0E56250067B69CBB33D35AFB559348121226558
len2=163 sha2=D9CB3BD5E84A8D80E44C2BA1B0E56250067B69CBB33D35AFB559348121226558
BYTE EQUAL: True
```

| Check | Result |
|---|---|
| First request status | 200 |
| Replay status | 200 |
| `len1` / `len2` | 163 / 163 |
| SHA-256 first | `D9CB3BD5...226558` |
| SHA-256 replay | `D9CB3BD5...226558` |
| Byte equality | **True** |
| `usage_event_id` first | 1 |
| `usage_event_id` replay | 1 |
| Usage row count | 1 |
| Rows for that key | 1 |

The first response is also byte-identical to the persisted `response_body`
string, so what the client receives initially is exactly what a retry receives.

A different key with an otherwise identical body produced a separate event
(`usage_event_id` 2) and took the count to 2 — one row per key, no collapse.

### Serialization fix

An earlier run of this probe found a real defect: the first response used
dict-insertion key order while the replay used `sort_keys=True`, so the two
differed in key order even though values matched. The route now serialises with
the same `json.dumps(..., sort_keys=True)` strategy used for the stored body, for
both the fresh and the replayed path. Two dedicated tests were added and pass:

- `test_first_and_replay_response_bytes_are_identical` — asserts
  `first.content == second.content`
- `test_first_response_matches_stored_body_exactly` — asserts the wire bytes
  equal the persisted `response_body`

---

## Probe 3 — API quota boundary

A dedicated tenant was created with 999 pre-existing `usage_events` rows, each
`api_calls = 1`, inserted directly into SQLite to establish the pre-boundary
state. No application code was modified.

```
=== PROBE C1: 999 existing + 1 request -> must SUCCEED at exactly 1000 ===
HTTP/1.1 200 OK
{"tenant_id":10,"completion":"dummy completion","usage_event_id":1002,"input_tokens":0,"cached_input_tokens":0,"output_tokens":0,"reasoning_tokens":0}

=== PROBE C2: 1000 existing + new key -> must be 429 ===
HTTP/1.1 429 Too Many Requests
{"detail":{"error":"quota_exceeded","message":"Tenant has exceeded its plan limit for api_calls. Used 1000 of 1000 this month; this request needs 1 more.","limit_type":"api_calls","used":1000,"limit":1000,"requested":1}}
```

| Check | Result |
|---|---|
| 999 + 1 | 200 (succeeds) |
| 1000 + 1 | 429 (rejected) |
| Rows created by rejected request | **0** |
| Resulting monthly `api_calls` | 1000 |

The boundary is inclusive at the limit: a tenant at exactly 1,000 calls is
allowed its last call; the next is refused. The check is
`used + requested > limit`.

---

## Probe 4 — Token quota boundary

A fresh tenant with zero prior usage sent a request for exactly the Free token
limit, then one token more.

```
=== PROBE C3: exactly 100000 tokens -> must SUCCEED ===
HTTP/1.1 200 OK
{"tenant_id":11,"completion":"dummy completion","usage_event_id":1003,"input_tokens":100000,"cached_input_tokens":0,"output_tokens":0,"reasoning_tokens":0}

=== PROBE C4: 100001 tokens (one beyond) -> must be 429 ===
HTTP/1.1 429 Too Many Requests
{"detail":{"error":"quota_exceeded","message":"Tenant has exceeded its plan limit for tokens. Used 100000 of 100000 this month; this request needs 100001 more.","limit_type":"tokens","used":100000,"limit":100000,"requested":100001}}
```

| Check | Result |
|---|---|
| Exactly 100,000 tokens | 200 (succeeds) |
| 100,001 tokens | 429 (rejected) |
| Rows created by rejected request | **0** |
| Resulting token usage | 100,000 |

The `tokens` total is `input_tokens + cached_input_tokens + output_tokens +
reasoning_tokens`; `cached_input_tokens` counts toward the limit.

---

## Probe 5 — Inactive subscription

A separate tenant was created, then its subscription status was set to
`inactive` by direct database setup.

```
=== subscription state ===
[(12, 1, 'inactive')]

POST /generate
X-Idempotency-Key: probeD-lapsed-0001
{"tenant_id": 12, "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_tokens": 0}

HTTP/1.1 402 Payment Required
{"detail":{"error":"subscription_inactive","message":"Tenant 12 has no active subscription. Billing is required before generating.","subscription_status":"inactive"}}
```

| Check | Result |
|---|---|
| Subscription status | `inactive` |
| Status | **402** |
| Error code | `subscription_inactive` |
| Usage rows for that tenant | **0** |
| Rows for `probeD-lapsed-0001` | **0** |

402 is distinct from 429: a lapsed subscription is a billing state problem, not
quota exhaustion. A test asserts the two are not conflated, by sending a
deliberately over-quota request to a lapsed tenant and requiring 402 rather than
429.

Because no row is written, the rejected key is never recorded and remains
retryable once the tenant is eligible again. Test
`test_rejected_request_does_not_consume_its_key` covers the 429 case explicitly:
the key is absent from the table after rejection and succeeds on retry.

---

## Probe 6 — Monthly usage

A previous-month row was inserted directly for tenant 2, then the rollup was
read.

```
GET /usage/2
HTTP/1.1 200 OK
{"tenant_id":2,"month_start":"2026-09-01T00:00:00Z","month_end":"2026-10-01T00:00:00Z","plan":"Free","api_calls_used":3,"api_calls_limit":1000,"tokens_used":510,"tokens_limit":100000}
```

Ground truth read from SQLite for the same window:

```
month_start: 2026-09-01 00:00:00   month_end (exclusive): 2026-10-01 00:00:00
IN-WINDOW  (rows, api_calls, tokens): (3, 3, 510)
BEFORE window (excluded rows):        (1, 50, 5000)
```

Rows for tenant 2, in full:

```
(1005, 'probeE-OLD-MONTH',  50, 5000, 0,  0,  0, '2026-08-31 23:59:59')
(   1, 'probeA-key-0001',    1,  100, 0, 50, 20, '2026-09-29 15:21:23')
(   2, 'probeB-key-0002',    1,  100, 0, 50, 20, '2026-09-29 15:21:41')
(1004, 'probeE-key-0003',    1,  100, 0, 50, 20, '2026-09-29 15:24:05')
```

| Field | API | DB ground truth | Match |
|---|---|---|---|
| `api_calls_used` | 3 | 3 | yes |
| `api_calls_limit` | 1000 | 1000 (Free) | yes |
| `tokens_used` | 510 | 510 | yes |
| `tokens_limit` | 100000 | 100000 (Free) | yes |

The row stamped `2026-08-31 23:59:59` — one second before `month_start` — is
excluded. The window is half-open `[month_start, month_end)`, so an event at
exactly `month_start` is included and one at exactly `month_end` is not. A test
covers both boundary instants. Server local time is never used; all timestamps
are naive UTC.

---

## Test suite

```
$ pytest -q
...........................                                              [100%]
27 passed, 1 warning in 4.46s
exit code: 0
```

One warning is a `StarletteDeprecationWarning` from FastAPI's `TestClient`
about `httpx` usage; it is upstream and does not affect results.

Coverage of the required scenarios:

| # | Scenario | Test(s) |
|---|---|---|
| 1 | Normal generation, one row | `test_generate_creates_exactly_one_usage_event` |
| 2 | Same key twice, identical response, one row | `test_same_key_twice_returns_identical_response_and_one_row`, `test_replay_does_not_increment_usage`, `test_first_and_replay_response_bytes_are_identical`, `test_first_response_matches_stored_body_exactly` |
| 3 | Different keys, two events | `test_different_keys_create_two_events` |
| 4 | Missing idempotency key → 400 | `test_missing_idempotency_key_is_400_and_creates_nothing`, `test_blank_idempotency_key_is_400` |
| 5 | API quota boundary | `test_api_quota_boundary_at_limit` |
| 6 | Token quota boundary | `test_token_quota_boundary_at_limit` |
| 7 | Rejected key not consumed | `test_rejected_request_does_not_consume_its_key` |
| 8 | Lapsed subscription → 402 | `test_inactive_subscription_is_402_and_creates_no_event`, `test_canceled_subscription_is_402`, `test_402_is_distinct_from_429` |
| 9 | Usage rollup, all four buckets | `test_usage_rollup_aggregates_all_four_token_buckets`, `test_usage_for_tenant_with_no_events_is_zero`, `test_usage_window_boundaries_are_utc_month` |
| 10 | Previous month excluded | `test_previous_month_events_are_excluded`, `test_exact_month_start_is_included` |
| 11 | Concurrent duplicate keys | `test_concurrent_identical_keys_create_exactly_one_event` (8 threads, one row, identical bodies) |
| 13 | Validation | `test_invalid_payloads_rejected_without_creating_events` (6 cases), `test_unknown_tenant_is_404` |

---

## Aggregate database state after the probe suite

```
  tenants        5
  plans          2
  subscriptions  5
  usage_events   1005
  stripe_events  0

duplicate idempotency keys: NONE
```

The 1,005 rows are 999 pre-seeded boundary rows plus 6 probe rows. `stripe_events`
is empty: the Stripe stage is not implemented.

---

## Fresh-clone verification (Stage 2.1)

Run after adding `seed.py` to the repository, to confirm a fresh clone is usable
without any file that existed only on the original machine. The repository was
cloned into a clean directory, `.env` was created from `.env.example`, and a
brand-new database was used. No local database, probe database, or untracked
file was involved.

```
$ git clone --branch main <repo> clone_sim/Billing_engine
clone exit: 0
```

Database initialization on a clean slate:

```
$ python seed.py
Seeded plans [Free (1), Pro (2)] and tenant Test Tenant <test@example.com>
seed exit: 0
```

Resulting schema and seed state, read from the new database:

```
tables: ['plans', 'stripe_events', 'subscriptions', 'tenants', 'usage_events']
plans: [(1, 'Free', 1000, 100000, 0), (2, 'Pro', 50000, 5000000, 2000)]
tenants: [(1, 'test@example.com', 1, 'active')]
subscriptions: [(1, 1, 'active')]
usage_events: 0
stripe_events: 0
```

Server startup from the clone:

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8002
GET /health -> 200 {"status":"ok"}
```

Endpoint checks against the fresh clone:

```
POST /tenants
HTTP/1.1 201 Created
{"id":2,"name":"Clone Sim Tenant","email":"clonesim@example.com","plan":"Free","status":"active"}

POST /generate
X-Idempotency-Key: clonesim-key-001
{"tenant_id": 2, "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50, "reasoning_tokens": 20}
HTTP/1.1 200 OK
{"cached_input_tokens": 0, "completion": "dummy completion", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20, "tenant_id": 2, "usage_event_id": 1}

GET /usage/2
HTTP/1.1 200 OK
{"tenant_id":2,"month_start":"2026-09-01T00:00:00Z","month_end":"2026-10-01T00:00:00Z","plan":"Free","api_calls_used":1,"api_calls_limit":1000,"tokens_used":170,"tokens_limit":100000}
```

`tokens_used` is 170 = 100 + 0 + 50 + 20, matching the request. A fresh clone
can therefore install, configure, seed, start, create a tenant, meter a request
and read usage using only committed files.

---

## STAGE 3 — STRIPE EVIDENCE

### Automated Stripe and regression tests

Executed from the committed Stage 3 tree with the project virtual environment:

```
$ .\\venv\\Scripts\\python.exe -m pytest -q
50 passed, 1 warning in 76.82s
```

The suite includes mocked Checkout API calls and SDK-generated valid webhook
signatures, plus invalid/tampered signatures, customer reuse, missing
configuration, unknown tenants/events, checkout completion, subscription
updates/deletion, database-backed duplicate delivery, and the existing Stage 2
metering tests. No real Stripe API call was made by pytest.

### Fresh-clone verification

The committed `main` tree was cloned to a new directory. The command sequence
created `.env` from `.env.example`, ran `pip install -r requirements.txt`, and
ran `seed.py` successfully:

```
Seeded plans [Free (1), Pro (2)] and tenant Test Tenant <test@example.com>
```

The clone's full suite then passed:

```
50 passed, 1 warning in 89.41s
```

A temporary uvicorn process from the clone was started and terminated after the
finite probe:

```
GET /health -> 200 {"status":"ok"}
```

### Real Stripe test-mode probe

```
Automated/mock Stripe tests: PASS
Real Stripe test-mode probe: BLOCKED / NOT RUN
Reason: no confirmed real Stripe test credentials or Stripe CLI were available;
the repository contains placeholders only, so no live result is claimed.
```

No `.env`, database, log, pid, cache, or secret file was staged in the Stage 3
commit. The working tree was clean after commit creation.
