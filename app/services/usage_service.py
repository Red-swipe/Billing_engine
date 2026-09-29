"""Metering: the single writer of usage_events.

Concurrency strategy
--------------------
Two separate problems, two separate mechanisms.

1. "Don't double-meter a retried key."
   The database UNIQUE constraint on usage_events.idempotency_key is the
   authority. A pre-insert SELECT is only a fast path to avoid raising an
   exception on the common sequential-retry case; correctness does NOT depend
   on it, because the INSERT still runs and still hits the constraint. Any
   IntegrityError on that constraint is caught, the session is rolled back, and
   the winner's committed row is read back and replayed.

2. "Don't let two concurrent requests both pass the quota gate."
   A SELECT-then-INSERT is a read-modify-write race, so the quota check and the
   INSERT are held under a process-wide reentrant lock. This is sufficient here
   because the app runs as a single uvicorn process against SQLite, which
   serialises writers anyway. A multi-process or multi-worker deployment would
   need a real database lock (SELECT ... FOR UPDATE, or BEGIN IMMEDIATE on
   SQLite) instead of a Python lock; that is out of scope for this stage and is
   called out in DESIGN.md.

Rejected requests never insert, so a rejected attempt leaves its idempotency
key unconsumed and retryable.
"""

import json
import threading
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Subscription, UsageEvent
from app.services.quota import check_quota, monthly_usage

# Guards the check-quota-then-insert critical section within this process.
_metering_lock = threading.RLock()


class SubscriptionInactive(Exception):
    """Tenant has no subscription, or it is not active."""

    def __init__(self, tenant_id: int, status: str | None):
        self.tenant_id = tenant_id
        self.status = status
        super().__init__(f"subscription not active for tenant {tenant_id}")


@dataclass
class MeteredResult:
    status_code: int
    body: dict
    replayed: bool


def build_response(
    tenant_id: int, usage_event_id: int, counts: dict, completion: str = "dummy completion"
) -> dict:
    """Deterministic success body. Same input row always serialises identically."""
    return {
        "tenant_id": tenant_id,
        "completion": completion,
        "usage_event_id": usage_event_id,
        "input_tokens": counts["input_tokens"],
        "cached_input_tokens": counts["cached_input_tokens"],
        "output_tokens": counts["output_tokens"],
        "reasoning_tokens": counts["reasoning_tokens"],
    }


def _replay(db: Session, key: str) -> MeteredResult | None:
    """Fast path: return the already-committed response for this key, if any."""
    row = db.execute(
        select(UsageEvent).where(UsageEvent.idempotency_key == key)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.response_body is None:
        # A row with no stored body cannot be replayed faithfully. Should not
        # happen: the row and its body are written in one transaction.
        raise RuntimeError(
            f"usage_events row {row.id} has no response_body and cannot be replayed"
        )
    return MeteredResult(
        status_code=row.response_status_code or 200,
        body=json.loads(row.response_body),
        replayed=True,
    )


def meter_request(
    db: Session,
    tenant_id: int,
    idempotency_key: str,
    counts: dict,
    api_calls: int = 1,
) -> MeteredResult:
    """Record one billable request, or replay the original if the key was seen.

    Order is authoritative: idempotency lookup -> subscription -> quota ->
    insert -> commit. The quota check happens BEFORE the insert, so a 429 or 402
    never creates a usage_events row.
    """
    with _metering_lock:
        # (a) Already seen? Replay the original status and body verbatim.
        existing = _replay(db, idempotency_key)
        if existing is not None:
            return existing

        # (b) Subscription must be active.
        subscription = db.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if subscription is None or subscription.status != "active":
            raise SubscriptionInactive(tenant_id, subscription.status if subscription else None)

        # (c) Quota, checked before anything is written.
        plan = subscription.plan
        tokens_requested = (
            counts["input_tokens"]
            + counts["cached_input_tokens"]
            + counts["output_tokens"]
            + counts["reasoning_tokens"]
        )
        api_calls_used, tokens_used = monthly_usage(db, tenant_id)
        check_quota(
            api_calls_used=api_calls_used,
            tokens_used=tokens_used,
            api_calls_limit=plan.api_calls_limit,
            tokens_limit=plan.tokens_limit,
            api_calls_requested=api_calls,
            tokens_requested=tokens_requested,
        )

        # (d) Insert the row. response_body needs the generated id, so flush
        # first, then set the body, then commit once.
        event = UsageEvent(
            tenant_id=tenant_id,
            api_calls=api_calls,
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cached_input_tokens"],
            output_tokens=counts["output_tokens"],
            reasoning_tokens=counts["reasoning_tokens"],
            idempotency_key=idempotency_key,
        )
        db.add(event)
        try:
            db.flush()
            body = build_response(tenant_id, event.id, counts)
            event.response_status_code = 200
            event.response_body = json.dumps(body, sort_keys=True)
            db.commit()
        except IntegrityError:
            # A concurrent request with the same key won the race. Undo our
            # partial work, then replay whatever it committed.
            db.rollback()
            winner = _replay(db, idempotency_key)
            if winner is None:
                raise
            return winner

        return MeteredResult(status_code=200, body=body, replayed=False)
