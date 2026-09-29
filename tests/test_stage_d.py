"""Stage 4 pricing and monthly cost tests."""

from datetime import timedelta

import pytest

from app.models import UsageEvent
from app.services.pricing import calculate_cost
from app.services.quota import current_month_window
from tests.conftest import key, make_tenant


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((0, 0, 0, 0), 0),
        ((1000, 0, 0, 0), 0),
        ((0, 1000, 0, 0), 0),
        ((0, 0, 1000, 0), 0),
        ((0, 0, 0, 1000), 0),
        ((1, 0, 0, 0), 0),
        ((999, 0, 0, 0), 0),
        ((1001, 0, 0, 0), 0),
        ((1500, 0, 0, 0), 0),
        ((0, 0, 1500, 0), 0),
        ((40000, 0, 0, 0), 1),
        ((0, 0, 0, 40000), 3),
        ((0, 0, 0, 2000000000), 150000),
    ],
)
def test_calculate_cost_exact_rates_and_fractional_units(counts, expected):
    assert calculate_cost(*counts) == expected


def test_calculate_cost_mixed_and_reasoning_is_not_double_counted():
    assert calculate_cost(1500, 2000, 3000, 4000) == 1
    assert calculate_cost(0, 0, 1000, 1000) == 0  # 0.15 cents rounds to 0
    assert calculate_cost(0, 0, 10000, 0) == 1


def test_calculate_cost_rounds_half_up():
    # 33,333 output tokens cost 2.499975 cents, so it rounds down; 33,334
    # costs 2.50005 cents, so it rounds up.
    assert calculate_cost(0, 0, 33333, 0) == 2
    assert calculate_cost(0, 0, 33334, 0) == 3


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_calculate_cost_rejects_invalid_token_counts(bad):
    with pytest.raises(ValueError):
        calculate_cost(bad, 0, 0, 0)


def test_usage_reports_monthly_cost_and_excludes_previous_month(client, session):
    tenant_id, _ = make_tenant(client)
    start, end = current_month_window()
    session.add_all(
        [
            UsageEvent(
                tenant_id=tenant_id,
                input_tokens=1000,
                cached_input_tokens=2000,
                output_tokens=3000,
                reasoning_tokens=4000,
                idempotency_key=key(),
                created_at=start + timedelta(days=1),
            ),
            UsageEvent(
                tenant_id=tenant_id,
                input_tokens=10_000_000,
                idempotency_key=key(),
                created_at=start - timedelta(seconds=1),
            ),
        ]
    )
    session.commit()

    before = session.query(UsageEvent).count()
    response = client.get(f"/usage/{tenant_id}")
    after = session.query(UsageEvent).count()

    assert response.status_code == 200
    assert response.json()["cost_cents"] == calculate_cost(1000, 2000, 3000, 4000)
    assert response.json()["tokens_used"] == 10_000
    assert before == after == 2
    assert end > start
