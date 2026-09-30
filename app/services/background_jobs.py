"""Small, in-process billing maintenance jobs.

The capstone deliberately avoids a distributed queue. FastAPI schedules these
jobs after the response is prepared; the bounded retry runner makes transient
failures observable without changing metering correctness.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Any

from fastapi import BackgroundTasks

from app.config import settings
from app.services.pricing import calculate_cost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobFailureAlert:
    job_name: str
    job_id: str
    attempts: int
    reason: str


def emit_failure_alert(alert: JobFailureAlert) -> None:
    """Default observable alert; applications may inject a test callback."""
    logger.error(
        "background_job_failed",
        extra={
            "job_name": alert.job_name,
            "job_id": alert.job_id,
            "attempts": alert.attempts,
            "reason": alert.reason,
        },
    )


def run_with_retries(
    job_name: str,
    job_id: str,
    operation: Callable[[], Any],
    *,
    max_attempts: int | None = None,
    alert_callback: Callable[[JobFailureAlert], None] = emit_failure_alert,
    sleep: Callable[[float], None] | None = None,
) -> Any | None:
    """Run an operation with bounded retries and one final failure alert."""
    attempts_allowed = (
        settings.BACKGROUND_JOB_MAX_ATTEMPTS
        if max_attempts is None
        else max_attempts
    )
    if attempts_allowed < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, attempts_allowed + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts_allowed:
                alert_callback(
                    JobFailureAlert(
                        job_name=job_name,
                        job_id=job_id,
                        attempts=attempt,
                        reason=str(exc),
                    )
                )
                return None
            if sleep is not None:
                sleep(0)
    return None


def reconcile_usage_cost(
    tenant_id: int,
    usage_event_id: int,
    counts: dict[str, int],
) -> int:
    """Reconcile one newly recorded event's deterministic billing cost.

    This is intentionally read-only: the authoritative usage row and quota
    accounting are completed synchronously before this maintenance job runs.
    """
    cost_cents = calculate_cost(
        counts["input_tokens"],
        counts["cached_input_tokens"],
        counts["output_tokens"],
        counts["reasoning_tokens"],
    )
    logger.info(
        "usage_cost_reconciled",
        extra={
            "tenant_id": tenant_id,
            "usage_event_id": usage_event_id,
            "cost_cents": cost_cents,
        },
    )
    return cost_cents


def schedule_usage_reconciliation(
    background_tasks: BackgroundTasks,
    tenant_id: int,
    usage_event_id: int,
    counts: dict[str, int],
) -> None:
    """Schedule reconciliation outside the immediate request handler."""
    background_tasks.add_task(
        run_with_retries,
        "usage_cost_reconciliation",
        str(usage_event_id),
        lambda: reconcile_usage_cost(tenant_id, usage_event_id, counts),
    )
