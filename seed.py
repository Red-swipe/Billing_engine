"""Idempotent seed. Safe to run repeatedly.

Usage: python seed.py
"""

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import Plan, Subscription, Tenant
from app.auth import hash_api_key

# DESIGN.md pins these ids: tenants.plan_id defaults to 1 (Free).
PLANS = [
    {
        "id": 1,
        "name": "Free",
        "api_calls_limit": 1_000,
        "tokens_limit": 100_000,
        "price_cents": 0,
    },
    {
        "id": 2,
        "name": "Pro",
        "api_calls_limit": 50_000,
        "tokens_limit": 5_000_000,
        "price_cents": 2_000,
    },
]

TEST_TENANT = {
    "name": "Test Tenant",
    "email": "test@example.com",
    "status": "active",
}
TEST_API_KEY = "test-tenant-api-key"


def seed() -> None:
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        for spec in PLANS:
            plan = db.get(Plan, spec["id"])
            if plan is None:
                db.add(Plan(**spec))
            else:
                for key, value in spec.items():
                    setattr(plan, key, value)

        db.commit()

        free_plan = db.execute(
            select(Plan).where(Plan.name == "Free")
        ).scalar_one_or_none()
        if free_plan is None:
            raise RuntimeError("Free plan missing after upsert")

        tenant = db.execute(
            select(Tenant).where(Tenant.email == TEST_TENANT["email"])
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                plan_id=free_plan.id,
                api_key_hash=hash_api_key(TEST_API_KEY),
                **TEST_TENANT,
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
        else:
            tenant.plan_id = free_plan.id
            tenant.api_key_hash = hash_api_key(TEST_API_KEY)

        subscription = db.execute(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        ).scalar_one_or_none()
        if subscription is None:
            db.add(
                Subscription(
                    tenant_id=tenant.id,
                    plan_id=free_plan.id,
                    status="active",
                )
            )
        else:
            subscription.plan_id = free_plan.id
            subscription.status = "active"

        db.commit()

        # Build the message while the session is still open. commit() expires
        # every instance, so reading an ORM attribute after this block closes
        # raises DetachedInstanceError.
        seeded_plans = ", ".join(
            f"{name} ({plan_id})"
            for name, plan_id in db.execute(
                select(Plan.name, Plan.id).order_by(Plan.id)
            ).all()
        )
        message = (
            f"Seeded plans [{seeded_plans}] and tenant "
            f"{TEST_TENANT['name']} <{TEST_TENANT['email']}>"
        )

    print(message)


if __name__ == "__main__":
    seed()
