from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_tenant
from app.models import Subscription, Tenant
from app.services.pricing import calculate_cost
from app.services.quota import current_month_window, monthly_token_usage, monthly_usage

router = APIRouter(tags=["usage"])


@router.get("/usage/{tenant_id}")
def get_usage(
    tenant_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Current UTC calendar-month usage, limits, and derived cost."""
    if tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not belong to the requested tenant",
        )

    subscription = tenant.subscription
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant {tenant_id} has no subscription",
        )
    plan = subscription.plan

    month_start, month_end = current_month_window()
    api_calls_used, tokens_used = monthly_usage(db, tenant_id)
    token_buckets = monthly_token_usage(db, tenant_id, (month_start, month_end))

    return {
        "tenant_id": tenant_id,
        "month_start": month_start.isoformat() + "Z",
        "month_end": month_end.isoformat() + "Z",
        "plan": plan.name,
        "api_calls_used": api_calls_used,
        "api_calls_limit": plan.api_calls_limit,
        "tokens_used": tokens_used,
        "tokens_limit": plan.tokens_limit,
        "cost_cents": calculate_cost(*token_buckets, api_calls=api_calls_used),
    }
