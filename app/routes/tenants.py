from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Plan, Subscription, Tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantCreate(BaseModel):
    name: str
    email: EmailStr


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)):
    existing = db.execute(
        select(Tenant).where(Tenant.email == payload.email)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"tenant with email {payload.email} already exists",
        )

    free_plan = db.execute(select(Plan).where(Plan.name == "Free")).scalar_one_or_none()
    if free_plan is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Free plan is not seeded; run seed.py",
        )

    tenant = Tenant(
        name=payload.name,
        email=payload.email,
        plan_id=free_plan.id,
        status="active",
    )
    db.add(tenant)
    # Same transaction: a tenant is never visible without its subscription,
    # so POST /generate can never hit the 402 state on a fresh tenant.
    db.add(
        Subscription(
            tenant=tenant,
            plan_id=free_plan.id,
            status="active",
        )
    )
    db.commit()
    db.refresh(tenant)

    return {
        "id": tenant.id,
        "name": tenant.name,
        "email": tenant.email,
        "plan": free_plan.name,
        "status": tenant.status,
    }
