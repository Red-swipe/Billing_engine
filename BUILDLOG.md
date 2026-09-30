# Build Log

## Item 4 - Tenant API-key isolation

The original API accepted `tenant_id` from `POST /generate` and allowed
unauthenticated usage and checkout access, so a caller could select another
tenant. Added a unique SHA-256 API-key hash to `Tenant`, a shared `X-API-Key`
dependency, one-time key issuance for new tenants, and the seeded development
key `test-tenant-api-key`. Generate, usage, and checkout now bind to the
authenticated tenant; Stripe webhooks remain signature-only.

Regression tests cover missing/invalid/valid keys, divergent tenant IDs,
cross-tenant usage, generation, checkout, and webhook independence. No
implementation mistake occurred during this change. The complete suite passed
with 75 tests.

Chronological record of what was built, why, and what was verified at each
stage. Only work that actually happened is recorded here.

---

## Phase 1 — Design and project setup

**Commit `38559ca`** — "Phase 1: design doc and project setup"

Four files: `.gitignore`, `.env.example`, `requirements.txt`, `DESIGN.md`.

### Decisions made

**`.gitignore` written first, before anything else.** `.env`, `__pycache__/`,
`*.pyc`, `.venv/`, `*.db`, `.pytest_cache/` were the required minimum. Added
`venv/` (the name actually used locally), `*.log`, `.DS_Store`, `Thumbs.db`.
Rationale: a committed `.env` persists in history even if deleted later.

**`stripe_events` column named `stripe_event_id`, not `event_id`.** The initial
`DESIGN.md` draft called it `event_id`. The brief specified `stripe_event_id`,
so the doc was corrected rather than the code following the doc. Now consistent
across `models.py` and `DESIGN.md`.

**Pro plan priced at 2,000 cents ($20.00).** Originally drafted as 2,900
($29.00) as a placeholder; corrected before any code was written.

**One `usage_events` row per request, rejecting the `type` + `quantity` ledger
design.** The reasoning is recorded in `DESIGN.md`: a type+quantity ledger
cannot represent the same token type twice in one request, fragments idempotency
across N rows, and buys a discriminator for a token set that is already closed
and known. One row makes one idempotency key map to exactly one row via a single
`UNIQUE` constraint.

**One explicit non-goal:** no refunds, no invoices, no proration.

### Verified

- `git status` confirmed `.env` was not listed after creating `.env.example`
- Committed and pushed to `origin/main`

---

## Stage 2 — Core billing logic

**Commit `bdd9322`** — "Stage 2: core billing logic", tagged `stage-b-done`

### Stage 2a — Models, schema, tenant creation

- All five models implemented per `DESIGN.md`, including `UNIQUE` on
  `usage_events.idempotency_key` and `stripe_events.stripe_event_id`
- Tables created via `Base.metadata.create_all` in the FastAPI lifespan;
  Alembic deliberately skipped
- `POST /tenants` creates the tenant **and** an active Free subscription in one
  transaction

**Deviation from the original `DESIGN.md`, and why.** The doc originally
described tenant creation without a subscription, which would leave every
API-created tenant in the `402` state on its first `POST /generate`. The
requirement is that a new tenant is on Free by default and immediately usable, so
the endpoint now creates both rows in one commit. `DESIGN.md` was updated to
describe the implemented behavior.

**`email-validator` added to requirements.** Discovered when the first live
`POST /tenants` request failed: `pydantic.EmailStr` requires it. Also a live
constraint — `email-validator` rejects reserved TLDs (`.test`, `.localhost`),
so those addresses cannot be used through the API.

### Stage 2b — Metering, idempotency, quota

Before writing code, two contradictions in `DESIGN.md` were resolved against the
brief:

1. **Idempotency key location.** The doc had it in the JSON body. The decision is
   `X-Idempotency-Key` header only, mandatory, `400` if absent, never
   server-generated.
2. **Order of operations.** The doc said insert-then-check-quota. That is wrong:
   a `429` would leave an orphan row that consumed quota, and would permanently
   consume the client's retry key. Corrected to
   validate → subscription → quota → insert → commit.

Both were corrected in `DESIGN.md` before implementation.

Implemented:

- `app/services/quota.py` — UTC calendar-month window (half-open
  `[start, end)`), aggregation across all four token buckets, and
  `check_quota` using `used + requested > limit` for inclusive-at-the-limit
  behavior
- `app/services/usage_service.py` — the only writer of `usage_events`. Runs the
  ordered sequence under a process-wide `RLock`, stores `response_body` and
  `response_status_code` in the same commit, and catches `IntegrityError` on the
  unique constraint to replay a concurrent winner
- `app/routes/generate.py`, `app/routes/usage.py`

**One schema field added:** `usage_events.response_status_code`. Storing only the
response body cannot satisfy "replay returns the same status code".

**Concurrency strategy.** Two separate problems with two mechanisms. Double-
metering is prevented by the database `UNIQUE` constraint; the pre-insert lookup
is a fast path only, and correctness does not depend on it. Quota overshoot is a
read-modify-write race, so the check and insert are held under a process-wide
lock — sufficient for the single-process uvicorn target. The multi-worker
limitation is documented rather than hidden.

### Stage 2c — Test suite

27 tests across 11 required scenarios, each on its own throwaway SQLite
database.

**Two environment problems, neither an application defect:**

1. `tmp_path` errored on setup because
   `C:\Users\<user>\AppData\Local\Temp\pytest-of-<user>` is access-denied on this
   machine. `conftest.py` allocates its own writable per-test directory instead.
2. A **real bug found by the tests**: `return result.body, result.status_code`.
   FastAPI serializes a returned tuple as a JSON *array*, not as
   `(body, status)`, producing `TypeError: list indices must be integers`. Three
   tests caught it. Fixed with an explicit `Response`.

### Stage 2d — Probes and evidence

Five probes against a live server on a temporary database. All passed. Raw output
preserved in `EVIDENCE.md`.

| Probe | Result |
|---|---|
| Normal generation | `200`, one row, `usage_event_id` 1 |
| Idempotency | Identical status and body, one row, second key produced a second event |
| API quota boundary | 999+1 → `200`; 1000+1 → `429`, zero rows |
| Token quota boundary | exactly 100,000 → `200`; 100,001 → `429`, zero rows |
| Inactive subscription | `402`, zero rows, key unconsumed |
| Monthly usage | API totals matched DB ground truth; previous-month row excluded |

**Post-gate fix.** Probe B revealed the first response used dict-insertion key
order while the replay used `sort_keys=True`, so the two differed in key order
even though values matched. Since the requirement is that a replay returns the
*original* response, the route now serialises with the same
`json.dumps(..., sort_keys=True)` strategy on both paths. Two byte-equality tests
were added. After the fix: both responses 200, 163 bytes, identical SHA-256,
same `usage_event_id`, one row, and the first response matches the persisted
`response_body` exactly.

Gate: `27 passed`, exit code 0. Committed, tagged `stage-b-done`, pushed.

---

## Stage 2.1 — Fresh-clone readiness

**Commit (this stage)** — "Stage 2.1: make fresh clone runnable", tagged
`stage-b-runnable`

Problem found during a repository readiness pass: `seed.py` was untracked. The
committed application depends on the Free plan existing — `POST /tenants` returns
`500 "Free plan is not seeded; run seed.py"` without it — so a fresh clone could
not run. `README.md`, `BUILDLOG.md` and `capstone.yaml` were also missing.

Changes:

- Added `seed.py`. Reviewed rather than rewritten: it already calls
  `create_all`, upserts both plans by pinned id, creates the test tenant and its
  subscription, reads `DATABASE_URL` from `.env`, and is idempotent. No secrets,
  no machine-specific paths, no dependency on probe databases.
- Added `README.md` — setup, database initialization, run, all three endpoints,
  testing, and a Stage 2 note that Stripe was not yet implemented at that time
- Added `BUILDLOG.md` (this file) and `capstone.yaml`
- Added `server.pid` to `.gitignore`
- Removed two genuinely unused imports (`os` in `tests/conftest.py`, `uuid` and
  `datetime` in `tests/test_stage_b.py`)

`DESIGN.md` and `EVIDENCE.md` were reviewed and left substantively intact; the
Stage 2 evidence was not retroactively edited.

Verified by a fresh-clone simulation: cloned the repository into a clean
directory, created `.env` from `.env.example`, and ran the documented setup
sequence against a brand-new database with no dependency on any local file.
Results are recorded in EVIDENCE.md.

`stage-b-done` was not moved or deleted.

---

## Stage 3 - Stripe integration

Implementation:

- Added environment-backed Stripe secret, webhook secret, Pro Price ID, and
  `APP_BASE_URL` configuration.
- Added `GET /checkout/{tenant_id}` with subscription-mode Checkout Sessions,
  explicit Free/active-Pro behavior, metadata mapping, and reusable customer
  persistence.
- Added raw-body `POST /webhooks/stripe` with official signature verification,
  database-backed unique event claiming, replay safety, and handlers for
  checkout completion plus subscription update/deletion.
- Added deterministic mocked Stripe tests covering configuration failures,
  customer reuse, invalid tenants, signature failures, deduplication, all
  supported events, unknown events, and Stage 2 regression behavior.

Decisions and limitations:

- Checkout does not upgrade local state; only a verified completion event does.
- The application uses persisted tenant/customer/subscription identifiers and
  never infers a tenant from arbitrary request values.
- Automated tests do not need credentials. Real Stripe test-mode verification
  is environment-dependent and is recorded separately in EVIDENCE.md.
- Pricing, cost rollups, and Alembic migrations remain Stage 4/future work.

Verification: full pytest suite and fresh-clone setup were run for this stage;
the exact results and any unavailable real-Stripe probe are recorded in
EVIDENCE.md.

## Stage 4 - Pricing and finalization

Implemented deterministic token pricing in `app/services/pricing.py` using exact
Decimal constants and integer-cent output. Input, cached-input, output, and
reasoning buckets use the Flyrank brief rates; reasoning is billed once with
output. Fractional 1,000-token quantities are preserved and the final value
uses documented half-up cent rounding. `/usage/{tenant_id}` now derives
`cost_cents` from current-month usage-event buckets without creating or
modifying usage.

Added 19 pricing, rounding, mixed-token, large-usage, monthly-window,
read-only, and regression assertions. Probe 5 matched an independent expected
cost of 1 cent for input=1,000, cached input=2,000, output=3,000, and
reasoning=4,000. The full suite passed with 70 tests. Fresh-clone setup and
cost verification are recorded in `EVIDENCE.md`; real Stripe test-mode
verification remains unavailable without confirmed credentials/CLI.

## Historical Stage 2 snapshot (superseded)

The following was accurate at the earlier Stage 2 checkpoint only. Stage 3
and Stage 4 below subsequently implemented Stripe integration and pricing.

- At that checkpoint, Stripe Checkout session creation was not yet built.
- At that checkpoint, the Stripe webhook receiver, signature verification, and
  event deduplication were not yet built.
- At that checkpoint, `subscription.updated` / `subscription.deleted` handling
  was not yet built.
- Alembic migrations

Alembic migrations remain outside this capstone stage and are listed in
`capstone.yaml` as the only remaining item.

## 2026-09-30 - Real Stripe verification checkpoint

Objective: complete Examiner Item 1 using the existing Stripe test-mode probe
and prove signature rejection plus database-backed webhook deduplication.

The existing probe tenant 2 completed a real Stripe Checkout subscription at
£10.00 GBP/month. Stripe reported a completed, paid subscription Checkout
Session, and the live CLI forwarded `checkout.session.completed`
`evt_1ULQ2iCSRoP1ezLYuediQy1r` to `POST /webhooks/stripe`, which returned HTTP
200. Tenant 2 became Pro with an active subscription, and `GET /usage/2`
returned HTTP 200 with `plan=Pro`.

The exact stored real webhook payload was sent once with an intentionally
forged signature and returned HTTP 400 with no change to the event count,
tenant, or subscription state. The same exact event body was then replayed
twice with a valid signature; both deliveries returned HTTP 200 with
`result=duplicate`. The original live delivery was the processed delivery;
the replay checks confirmed exactly one `stripe_events` row and no duplicate
subscription state.

Issue discovered: the Checkout success redirect points to
`http://localhost:8000/`, which is not an implemented API route and therefore
returns HTTP 404 after the successful payment and webhook processing. No
application code was changed for this checkpoint; the redirect is documented
as a known presentation/integration issue.

Regression result: `70 passed, 1 warning`. The warning is the existing
Starlette/httpx deprecation warning. `billing.db` was not used or modified.
