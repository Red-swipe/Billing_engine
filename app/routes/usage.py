from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Subscription, Tenant
from app.services.quota import current_month_window, monthly_usage

router = APIRouter(tags=["usage"])


@router.get("/usage/{tenant_id}")
def get_usage(tenant_id: int, db: Session = Depends(get_db)):
    """Current UTC calendar-month usage against the tenant's plan limits.

    No pricing or cost: that is a later stage.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant {tenant_id} does not exist",
        )

    subscription = db.get(Subscription, tenant_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant {tenant_id} has no subscription",
        )
    plan = subscription.plan

    month_start, month_end = current_month_window()
    api_calls_used, tokens_used = monthly_usage(db, tenant_id)

    return {
        "tenant_id": tenant_id,
        "month_start": month_start.isoformat() + "Z",
        "month_end": month_end.isoformat() + "Z",
        "plan": plan.name,
        "api_calls_used": api_calls_used,
        "api_calls_limit": plan.api_calls_limit,
        "tokens_used": tokens_used,
        "tokens_limit": plan.tokens_limit,
    }
