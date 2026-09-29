"""Stage B tests: /generate idempotency, quota, lapsed subscription, usage rollup."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Subscription, UsageEvent
from app.services.quota import current_month_window
from tests.conftest import key, make_tenant


def usage_count(session, tenant_id=None):
    if tenant_id is None:
        return session.execute(select(func.count(UsageEvent.id))).scalar_one()
    return session.execute(
        select(func.count(UsageEvent.id)).where(UsageEvent.tenant_id == tenant_id)
    ).scalar_one()


# ---------------------------------------------------------------- Test 1


def test_generate_creates_exactly_one_usage_event(client, session):
    tenant_id, _ = make_tenant(client)
    payload_in = {"tenant_id": tenant_id, "input_tokens": 10, "output_tokens": 5}

    resp = client.post(
        "/generate", json=payload_in, headers={"X-Idempotency-Key": key()}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    assert body["tenant_id"] == tenant_id
    assert body["usage_event_id"] > 0
    assert usage_count(session) == 1


# ---------------------------------------------------------------- Test 2


def test_same_key_twice_returns_identical_response_and_one_row(client, session):
    tenant_id, _ = make_tenant(client)
    k = key()
    body = {"tenant_id": tenant_id, "input_tokens": 10, "output_tokens": 5}

    first = client.post("/generate", json=body, headers={"X-Idempotency-Key": k})
    second = client.post("/generate", json=body, headers={"X-Idempotency-Key": k})

    assert first.status_code == 200
    assert second.status_code == first.status_code
    assert second.json() == first.json()
    assert usage_count(session) == 1


def test_first_and_replay_response_bytes_are_identical(client, session):
    """The bytes the client first receives must equal the bytes a retry gets.

    Guards the serialisation strategy: the stored response_body is written with
    sort_keys=True, so the live response must use the same strategy or the two
    differ in key order.
    """
    tenant_id, _ = make_tenant(client)
    k = key()
    payload_in = {"tenant_id": tenant_id, "input_tokens": 10, "output_tokens": 5}

    first = client.post(
        "/generate", json=payload_in, headers={"X-Idempotency-Key": k}
    )
    second = client.post(
        "/generate", json=payload_in, headers={"X-Idempotency-Key": k}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content, (
        f"first bytes != replay bytes\n  first : {first.content!r}\n"
        f"  replay: {second.content!r}"
    )
    assert first.json()["usage_event_id"] == second.json()["usage_event_id"]
    assert usage_count(session) == 1


def test_first_response_matches_stored_body_exactly(client, session):
    """The wire bytes must equal the persisted response_body string."""
    tenant_id, _ = make_tenant(client)
    k = key()
    resp = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 7},
        headers={"X-Idempotency-Key": k},
    )
    assert resp.status_code == 200

    stored = session.execute(
        select(UsageEvent.response_body).where(UsageEvent.idempotency_key == k)
    ).scalar_one()
    assert resp.content.decode("utf-8") == stored
    assert usage_count(session) == 1


def test_replay_does_not_increment_usage(client, session):
    tenant_id, _ = make_tenant(client)
    k = key()
    body = {"tenant_id": tenant_id, "input_tokens": 10}

    client.post("/generate", json=body, headers={"X-Idempotency-Key": k})
    for _ in range(5):
        client.post("/generate", json=body, headers={"X-Idempotency-Key": k})

    assert usage_count(session) == 1
    resp = client.get(f"/usage/{tenant_id}")
    assert resp.json()["api_calls_used"] == 1


# ---------------------------------------------------------------- Test 3


def test_different_keys_create_two_events(client, session):
    tenant_id, _ = make_tenant(client)
    body = {"tenant_id": tenant_id, "input_tokens": 10}

    a = client.post("/generate", json=body, headers={"X-Idempotency-Key": key()})
    b = client.post("/generate", json=body, headers={"X-Idempotency-Key": key()})

    assert a.status_code == 200
    assert b.status_code == 200
    assert a.json()["usage_event_id"] != b.json()["usage_event_id"]
    assert usage_count(session) == 2


# ---------------------------------------------------------------- Test 4


def test_missing_idempotency_key_is_400_and_creates_nothing(client, session):
    tenant_id, _ = make_tenant(client)
    body = {"tenant_id": tenant_id, "input_tokens": 10}

    resp = client.post("/generate", json=body)

    assert resp.status_code == 400
    assert "X-Idempotency-Key" in resp.text
    assert usage_count(session) == 0


def test_blank_idempotency_key_is_400(client, session):
    tenant_id, _ = make_tenant(client)
    resp = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 1},
        headers={"X-Idempotency-Key": "   "},
    )
    assert resp.status_code == 400
    assert usage_count(session) == 0


# ---------------------------------------------------------------- Test 5


def test_api_quota_boundary_at_limit(client, session):
    """999 existing calls -> the next succeeds (exactly 1000) -> next is 429."""
    tenant_id, _ = make_tenant(client)

    start, end = current_month_window()
    for i in range(999):
        session.add(
            UsageEvent(
                tenant_id=tenant_id,
                api_calls=1,
                idempotency_key=f"pre-{i}",
                created_at=start + timedelta(hours=1),
            )
        )
    session.commit()
    assert usage_count(session) == 999

    # 999 -> 1000: exactly at the limit, must succeed.
    at_limit = client.post(
        "/generate",
        json={"tenant_id": tenant_id},
        headers={"X-Idempotency-Key": key()},
    )
    assert at_limit.status_code == 200, at_limit.text
    assert usage_count(session) == 1000

    # 1000 -> 1001: over the limit, must be rejected and write nothing.
    over = client.post(
        "/generate",
        json={"tenant_id": tenant_id},
        headers={"X-Idempotency-Key": key()},
    )
    assert over.status_code == 429
    detail = over.json()["detail"]
    assert detail["limit_type"] == "api_calls"
    assert detail["used"] == 1000
    assert detail["limit"] == 1000
    assert usage_count(session) == 1000


# ---------------------------------------------------------------- Test 6


def test_token_quota_boundary_at_limit(client, session):
    """Exactly at tokens_limit succeeds; one token beyond is 429."""
    tenant_id, _ = make_tenant(client)
    start, _end = current_month_window()

    # Seed to exactly 100_000 - 100 = 99_900, then send 100 -> exactly 100_000.
    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=1,
            input_tokens=99_900,
            idempotency_key="pre-tokens",
            created_at=start + timedelta(hours=1),
        )
    )
    session.commit()

    at_limit = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 100},
        headers={"X-Idempotency-Key": key()},
    )
    assert at_limit.status_code == 200, at_limit.text

    usage = client.get(f"/usage/{tenant_id}").json()
    assert usage["tokens_used"] == 100_000
    assert usage["tokens_limit"] == 100_000

    over = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 1},
        headers={"X-Idempotency-Key": key()},
    )
    assert over.status_code == 429
    assert over.json()["detail"]["limit_type"] == "tokens"
    assert usage_count(session) == 2  # the rejected attempt added nothing


# ---------------------------------------------------------------- Test 7


def test_rejected_request_does_not_consume_its_key(client, session):
    """A 429 must leave the key unconsumed so a later retry can succeed."""
    tenant_id, _ = make_tenant(client)
    k = key()
    start, _end = current_month_window()

    # Fill the tenant to exactly the API limit so the next call is rejected.
    for i in range(1_000):
        session.add(
            UsageEvent(
                tenant_id=tenant_id,
                api_calls=1,
                idempotency_key=f"full-{i}",
                created_at=start + timedelta(hours=1),
            )
        )
    session.commit()

    rejected = client.post(
        "/generate", json={"tenant_id": tenant_id}, headers={"X-Idempotency-Key": k}
    )
    assert rejected.status_code == 429
    assert usage_count(session) == 1_000
    # The rejected key must not exist in the table at all.
    assert (
        session.execute(
            select(func.count(UsageEvent.id)).where(UsageEvent.idempotency_key == k)
        ).scalar_one()
        == 0
    )

    # Tenant becomes eligible again (new month: clear the old rows).
    session.query(UsageEvent).delete()
    session.commit()

    retry = client.post(
        "/generate", json={"tenant_id": tenant_id}, headers={"X-Idempotency-Key": k}
    )
    assert retry.status_code == 200, retry.text
    assert usage_count(session) == 1


# ---------------------------------------------------------------- Test 8


def test_inactive_subscription_is_402_and_creates_no_event(client, session):
    tenant_id, _ = make_tenant(client)
    k = key()

    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    sub.status = "inactive"
    session.commit()

    resp = client.post(
        "/generate", json={"tenant_id": tenant_id}, headers={"X-Idempotency-Key": k}
    )

    assert resp.status_code == 402
    assert "subscription" in resp.text.lower()
    assert usage_count(session) == 0


def test_canceled_subscription_is_402(client, session):
    tenant_id, _ = make_tenant(client)
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    sub.status = "canceled"
    session.commit()

    resp = client.post(
        "/generate",
        json={"tenant_id": tenant_id},
        headers={"X-Idempotency-Key": key()},
    )
    assert resp.status_code == 402
    assert usage_count(session) == 0


def test_402_is_distinct_from_429(client, session):
    """A lapsed subscription must not be reported as quota exhaustion."""
    tenant_id, _ = make_tenant(client)
    sub = session.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one()
    sub.status = "inactive"
    session.commit()

    resp = client.post(
        "/generate",
        json={"tenant_id": tenant_id, "input_tokens": 999_999},
        headers={"X-Idempotency-Key": key()},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["error"] == "subscription_inactive"


# ---------------------------------------------------------------- Test 9


def test_usage_rollup_aggregates_all_four_token_buckets(client, session):
    tenant_id, _ = make_tenant(client)
    start, _end = current_month_window()

    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=1,
            input_tokens=10,
            cached_input_tokens=20,
            output_tokens=30,
            reasoning_tokens=40,
            idempotency_key="agg-1",
            created_at=start + timedelta(hours=2),
        )
    )
    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=1,
            input_tokens=1,
            cached_input_tokens=2,
            output_tokens=3,
            reasoning_tokens=4,
            idempotency_key="agg-2",
            created_at=start + timedelta(hours=3),
        )
    )
    session.commit()

    resp = client.get(f"/usage/{tenant_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_calls_used"] == 2
    assert body["api_calls_limit"] == 1_000
    assert body["tokens_used"] == (10 + 20 + 30 + 40) + (1 + 2 + 3 + 4)
    assert body["tokens_limit"] == 100_000
    assert body["plan"] == "Free"


def test_usage_for_tenant_with_no_events_is_zero(client, session):
    tenant_id, _ = make_tenant(client)
    body = client.get(f"/usage/{tenant_id}").json()
    assert body["api_calls_used"] == 0
    assert body["tokens_used"] == 0


def test_usage_window_boundaries_are_utc_month(client, session):
    tenant_id, _ = make_tenant(client)
    body = client.get(f"/usage/{tenant_id}").json()
    assert body["month_start"].endswith("-01T00:00:00Z")
    assert body["month_end"].endswith("-01T00:00:00Z")


# --------------------------------------------------------------- Test 10


def test_previous_month_events_are_excluded(client, session):
    tenant_id, _ = make_tenant(client)
    start, end = current_month_window()

    # Last instant of the previous month.
    last_instant_previous = start - timedelta(seconds=1)
    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=5,
            input_tokens=500,
            idempotency_key="old",
            created_at=last_instant_previous,
        )
    )
    # First instant of the current month.
    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=2,
            input_tokens=20,
            idempotency_key="new",
            created_at=start,
        )
    )
    session.commit()

    body = client.get(f"/usage/{tenant_id}").json()
    assert body["api_calls_used"] == 2
    assert body["tokens_used"] == 20


def test_exact_month_start_is_included(client, session):
    tenant_id, _ = make_tenant(client)
    start, end = current_month_window()

    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=1,
            idempotency_key="at-start",
            created_at=start,
        )
    )
    session.add(
        UsageEvent(
            tenant_id=tenant_id,
            api_calls=1,
            idempotency_key="at-end",
            created_at=end,  # exclusive boundary: must NOT count
        )
    )
    session.commit()

    body = client.get(f"/usage/{tenant_id}").json()
    assert body["api_calls_used"] == 1


# --------------------------------------------------------------- Test 11


def test_concurrent_identical_keys_create_exactly_one_event(client, session):
    """Concurrent duplicate keys must not double-meter.

    Uses threads against the same TestClient. The production code holds the
    quota+insert critical section under a process-wide lock and relies on the
    DB UNIQUE constraint, so exactly one row is committed and every caller sees
    the same body.
    """
    import threading

    tenant_id, _ = make_tenant(client)
    k = key()
    body = {"tenant_id": tenant_id, "input_tokens": 10}

    results = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post("/generate", json=body, headers={"X-Idempotency-Key": k})
            with lock:
                results.append((r.status_code, r.json()))
        except Exception as exc:  # recorded, asserted below
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent requests raised: {errors}"
    assert len(results) == 8
    assert usage_count(session) == 1, f"double-metered: {usage_count(session)} rows"

    statuses = {s for s, _ in results}
    bodies = {r["usage_event_id"] for _, r in results}
    assert statuses == {200}, statuses
    assert len(bodies) == 1, f"callers saw different responses: {bodies}"


# ----------------------------------------------------------- validation


@pytest.mark.parametrize(
    "body",
    [
        {"tenant_id": 1, "input_tokens": -1},
        {"tenant_id": 1, "output_tokens": -5},
        {"tenant_id": 1, "reasoning_tokens": -1},
        {"tenant_id": 1, "input_tokens": 1.5},
        {"tenant_id": 0},
        {"tenant_id": -1},
    ],
)
def test_invalid_payloads_rejected_without_creating_events(client, session, body):
    resp = client.post("/generate", json=body, headers={"X-Idempotency-Key": key()})
    assert resp.status_code == 422
    assert usage_count(session) == 0


def test_unknown_tenant_is_404(client, session):
    resp = client.post(
        "/generate",
        json={"tenant_id": 999_999, "input_tokens": 1},
        headers={"X-Idempotency-Key": key()},
    )
    assert resp.status_code == 404
    assert usage_count(session) == 0
