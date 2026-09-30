"""GET /checkout/{tenant_id} — create a Stripe Checkout Session for the Pro plan.

Note this is a GET because the brief specifies it. It does mutate state (it may
create a Stripe customer), which is unusual REST; a POST would be more correct
in general, but the endpoint shape here is fixed by the specification.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.auth import get_current_tenant
from app.database import get_db
from app.models import Plan, Subscription, Tenant
from app.services.stripe_service import (
    StripeConfigurationError,
    create_checkout_session,
)

router = APIRouter(tags=["checkout"])


@router.get("/checkout/{tenant_id}")
def create_checkout(
    tenant_id: int,
    db: Session = Depends(get_db),
    authenticated_tenant: Tenant = Depends(get_current_tenant),
):
    """Return a Checkout URL for upgrading this tenant to Pro.

    Already-Pro tenants get an explicit 409 rather than a silently created
    second session: the upgrade is already done, and a fresh session would be a
    second paid subscription for the same tenant.
    """
    if tenant_id != authenticated_tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not belong to the requested tenant",
        )
    tenant = authenticated_tenant

    subscription = db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    ).scalar_one_or_none()

    current_plan = tenant.plan
    if current_plan is not None and current_plan.name == "Pro" and (
        subscription is not None and subscription.status == "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_pro",
                "message": (
                    f"Tenant {tenant_id} is already on an active Pro subscription. "
                    "No upgrade session was created."
                ),
                "plan": current_plan.name,
                "subscription_status": subscription.status,
            },
        )

    pro_plan = db.execute(select(Plan).where(Plan.name == "Pro")).scalar_one_or_none()
    if pro_plan is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pro plan is not seeded; run seed.py",
        )

    if not settings.stripe_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Stripe is not configured. Set STRIPE_SECRET_KEY and "
                "STRIPE_PRO_PRICE_ID in .env."
            ),
        )

    try:
        session_obj = create_checkout_session(db, tenant, pro_plan)
    except StripeConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:  # stripe SDK raises its own error hierarchy
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe checkout session creation failed: {exc}",
        ) from exc

    # Never return the secret key. Only the public session id and URL.
    return {
        "tenant_id": tenant_id,
        "plan": pro_plan.name,
        "price_cents": pro_plan.price_cents,
        "checkout_session_id": session_obj.id,
        "checkout_url": session_obj.url,
    }
