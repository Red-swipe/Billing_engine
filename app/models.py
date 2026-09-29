from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    """Naive UTC.

    SQLite has no native timestamp type and drops tzinfo on read, so storing
    aware datetimes would silently mix aware and naive values and break the
    calendar-month range queries. Everything in this codebase is naive UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    api_calls_limit: Mapped[int] = mapped_column(nullable=False)
    tokens_limit: Mapped[int] = mapped_column(nullable=False)
    price_cents: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    tenants: Mapped[list["Tenant"]] = relationship(back_populates="plan")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    plan: Mapped["Plan"] = relationship(back_populates="tenants")
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="tenant", uselist=False
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_subscriptions_tenant_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False)
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(nullable=False, default="inactive")
    current_period_start: Mapped[datetime | None] = mapped_column(nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    tenant: Mapped["Tenant"] = relationship(back_populates="subscription")
    plan: Mapped["Plan"] = relationship()


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    api_calls: Mapped[int] = mapped_column(nullable=False, default=1)
    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Minimal field so a replayed request returns the original HTTP status as
    # well as the original body. Without it, "same status code" is not
    # representable. Set in the same transaction as response_body.
    response_status_code: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=utcnow, index=True
    )


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    __table_args__ = (
        UniqueConstraint("stripe_event_id", name="uq_stripe_events_stripe_event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    processed: Mapped[bool] = mapped_column(nullable=False, default=False)
    # When the business operation for this event was applied. Distinguishes
    # "received but failed" (processed=false) from "fully applied"
    # (processed=true, processed_at set).
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
