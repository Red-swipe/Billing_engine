"""Item 5 background execution, retry, and failure-alert tests."""

import asyncio

from fastapi import BackgroundTasks

from app.services import background_jobs


def test_usage_reconciliation_is_scheduled_and_executed(monkeypatch):
    executed = []

    def fake_reconcile(tenant_id, usage_event_id, counts):
        executed.append((tenant_id, usage_event_id, counts))
        return 7

    monkeypatch.setattr(background_jobs, "reconcile_usage_cost", fake_reconcile)
    tasks = BackgroundTasks()
    background_jobs.schedule_usage_reconciliation(
        tasks, 41, 99, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_tokens": 0}
    )

    assert len(tasks.tasks) == 1
    asyncio.run(tasks())
    assert executed == [(41, 99, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_tokens": 0})]


def test_successful_job_does_not_retry():
    calls = []
    alerts = []

    result = background_jobs.run_with_retries(
        "test_job", "job-1", lambda: calls.append(1) or "done",
        max_attempts=3, alert_callback=alerts.append,
    )

    assert result == "done"
    assert calls == [1]
    assert alerts == []


def test_transient_failure_retries_then_stops_on_success():
    calls = []
    alerts = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("temporary billing read failure")
        return "reconciled"

    result = background_jobs.run_with_retries(
        "test_job", "job-2", flaky, max_attempts=3,
        alert_callback=alerts.append,
    )

    assert result == "reconciled"
    assert len(calls) == 3
    assert alerts == []


def test_permanent_failure_is_bounded_and_alerts_once_after_final_attempt():
    calls = []
    alerts = []
    observations = []

    def always_fails():
        calls.append(1)
        observations.append(len(alerts))
        raise ValueError("billing provider unavailable")

    result = background_jobs.run_with_retries(
        "usage_cost_reconciliation", "event-123", always_fails,
        max_attempts=3, alert_callback=alerts.append,
    )

    assert result is None
    assert len(calls) == 3
    assert observations == [0, 0, 0]
    assert len(alerts) == 1
    assert alerts[0].job_name == "usage_cost_reconciliation"
    assert alerts[0].job_id == "event-123"
    assert alerts[0].attempts == 3
    assert "provider unavailable" in alerts[0].reason


def test_retry_count_is_configurable_and_successful_retry_has_no_alert():
    calls = []
    alerts = []

    def succeeds_on_second_attempt():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return 42

    result = background_jobs.run_with_retries(
        "test_job", "job-3", succeeds_on_second_attempt,
        max_attempts=2, alert_callback=alerts.append,
    )

    assert result == 42
    assert len(calls) == 2
    assert alerts == []
