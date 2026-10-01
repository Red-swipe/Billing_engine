"""Stripe integration: Checkout sessions, customer mapping, webhook handling.

Design notes
------------
**Tenant mapping.** A webhook must never upgrade the wrong tenant, so the
tenant is identified only from data this application itself wrote:

1. `checkout.session.completed` resolves the tenant from the Checkout Session's
   ``metadata.tenant_id``, which ``GET /checkout/{tenant_id}`` wrote at session
   creation time. It falls back to ``client_reference_id`` and finally to
   ``customer`` -> ``tenants.stripe_customer_id``.
2. ``customer.subscription.*`` events carry no session metadata, so they resolve
   through ``subscriptions.stripe_subscription_id`` and then
   ``tenants.stripe_customer_id`` — both written by us.

An event that resolves to no known tenant is recorded as received but
**unprocessed** and acknowledged, rather than being applied to a guess.

**Deduplication.** The database is the source of truth. ``stripe_events`` carries
``UNIQUE(stripe_event_id)``, and a row is only written *after* the business
operation succeeds, in the same transaction. A duplicate delivery therefore hits
the unique constraint, is acknowledged, and cannot re-apply the upgrade. No
in-memory set is involved, so this holds across processes and restarts.

**Ordering and rollback.** Handler and event row are committed together. If the
handler raises, the row is rolled back, so Stripe retries the delivery and the
operation is not half-applied.
"""

import json
from datetime import datetime

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Plan, StripeEvent, Subscription, Tenant, utcnow

# Stripe subscription statuses that mean the tenant is currently entitled to
# generate. Mirrors the Stage 2 `subscriptions.status` vocabulary.
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


class StripeConfigurationError(Exception):
    """Stripe credentials or price id are missing."""


class WebhookSignatureError(Exception):
    """The Stripe-Signature header did not verify against the raw body."""


def _get(obj, key, default=None):
    """Read a field from either a plain dict or a stripe.StripeObject.

    stripe 15.x's StripeObject deliberately blocks dict methods like ``.get()``,
    so the handlers cannot assume dict semantics. Indexing works on both, so we
    route through that and swallow the lookup failure.
    """
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, IndexError, TypeError):
        return default


def configure_stripe() -> None:
    """Point the Stripe SDK at the configured secret key."""
    if not settings.STRIPE_SECRET_KEY:
        raise StripeConfigurationError("STRIPE_SECRET_KEY is not set")
    stripe.api_key = settings.STRIPE_SECRET_KEY


def get_or_create_customer(db: Session, tenant: Tenant) -> str:
    """Return the tenant's Stripe customer id, creating and persisting one if needed.

    Reuse is the point: a tenant that already has ``stripe_customer_id`` never
    triggers a second ``customers.create`` call, so repeated ``/checkout`` hits
    do not fragment a customer into duplicates.
    """
    if tenant.stripe_customer_id:
        return tenant.stripe_customer_id

    configure_stripe()
    customer = stripe.Customer.create(
        name=tenant.name,
        email=tenant.email,
        # Traceable back to our row even if metadata is ever dropped.
        metadata={"tenant_id": str(tenant.id)},
    )
    tenant.stripe_customer_id = customer.id
    db.commit()
    db.refresh(tenant)
    return customer.id


def create_checkout_session(db: Session, tenant: Tenant, pro_plan: Plan) -> dict:
    """Create a subscription-mode Checkout Session for the Pro plan.

    ``metadata.tenant_id`` is the primary key back to our tenant for the
    resulting webhook. ``client_reference_id`` is set to the same value as a
    second, independently-readable signal.
    """
    if not settings.STRIPE_PRO_PRICE_ID:
        raise StripeConfigurationError("STRIPE_PRO_PRICE_ID is not set")

    customer_id = get_or_create_customer(db, tenant)
    configure_stripe()

    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": settings.STRIPE_PRO_PRICE_ID, "quantity": 1}],
        success_url=f"{settings.APP_BASE_URL}/",
        cancel_url=f"{settings.APP_BASE_URL}/?checkout=cancelled",
        metadata={"tenant_id": str(tenant.id), "plan": pro_plan.name},
        client_reference_id=str(tenant.id),
        # Portal access is only useful for a real subscription; harmless here
        # and keeps the session self-describing.
        subscription_data={"metadata": {"tenant_id": str(tenant.id)}},
    )


def construct_event(raw_body: bytes, signature_header: str) -> stripe.Event:
    """Verify the Stripe signature and return the parsed event.

    Uses the SDK's official verification, which recomputes an HMAC over the
    *raw* bytes. A tampered body or a forged signature raises
    ``WebhookSignatureError`` and the caller must not process the event.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise WebhookSignatureError("STRIPE_WEBHOOK_SECRET is not set")
    if not signature_header:
        raise WebhookSignatureError("Stripe-Signature header missing")
    try:
        return stripe.Webhook.construct_event(
            raw_body, signature_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        # stripe 15.x raises SignatureVerificationError for a bad signature and
        # ValueError for a malformed header/payload. Both mean "do not trust
        # this request", so both collapse to a 400 at the route.
        raise WebhookSignatureError(str(exc)) from exc


def _plan_by_name(db: Session, name: str) -> Plan | None:
    return db.execute(select(Plan).where(Plan.name == name)).scalar_one_or_none()


def _resolve_tenant_from_session(db: Session, session_obj) -> Tenant | None:
    """metadata.tenant_id -> client_reference_id -> customer id."""
    metadata = _get(session_obj, "metadata") or {}
    raw = _get(metadata, "tenant_id")
    if raw and str(raw).isdigit():
        tenant = db.get(Tenant, int(raw))
        if tenant is not None:
            return tenant

    reference = _get(session_obj, "client_reference_id")
    if reference and str(reference).isdigit():
        tenant = db.get(Tenant, int(reference))
        if tenant is not None:
            return tenant

    customer_id = _get(session_obj, "customer")
    if customer_id:
        return db.execute(
            select(Tenant).where(Tenant.stripe_customer_id == customer_id)
        ).scalar_one_or_none()
    return None


def _resolve_tenant_from_subscription(db: Session, sub_obj) -> Tenant | None:
    """stripe_subscription_id -> stripe_customer_id -> session metadata."""
    subscription_id = _get(sub_obj, "id")
    if subscription_id:
        row = db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == subscription_id
            )
        ).scalar_one_or_none()
        if row is not None:
            return row.tenant

    customer_id = _get(sub_obj, "customer")
    if customer_id:
        return db.execute(
            select(Tenant).where(Tenant.stripe_customer_id == customer_id)
        ).scalar_one_or_none()

    metadata = _get(sub_obj, "metadata") or {}
    raw = _get(metadata, "tenant_id")
    if raw and str(raw).isdigit():
        return db.get(Tenant, int(raw))
    return None


def _apply_subscription_state(
    db: Session, tenant: Tenant, sub_obj, *, activate: bool
) -> None:
    """Write Stripe's subscription state onto our subscription row.

    ``activate=False`` marks the tenant inactive without deleting history, which
    is what Stage 2's ``402`` path keys off.
    """
    subscription = db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    ).scalar_one_or_none()

    status = _get(sub_obj, "status", "")
    is_active = activate and status in ACTIVE_SUBSCRIPTION_STATUSES

    if subscription is None:
        subscription = Subscription(tenant_id=tenant.id, plan_id=tenant.plan_id)
        db.add(subscription)

    if is_active:
        pro = _plan_by_name(db, "Pro")
        if pro is not None:
            subscription.plan_id = pro.id
            tenant.plan_id = pro.id
        subscription.status = "active"
        tenant.status = "active"
    else:
        subscription.status = "canceled"
        tenant.status = "inactive"

    subscription.stripe_subscription_id = _get(sub_obj, "id")
    subscription.current_period_start = _epoch_to_naive_utc(
        _get(sub_obj, "current_period_start")
    )
    subscription.current_period_end = _epoch_to_naive_utc(
        _get(sub_obj, "current_period_end")
    )
    db.flush()


def _epoch_to_naive_utc(value) -> datetime | None:
    """Stripe sends period boundaries as Unix seconds; our convention is naive UTC."""
    if value is None:
        return None
    return datetime.utcfromtimestamp(int(value))


def handle_checkout_session_completed(db: Session, event: stripe.Event) -> bool:
    """Free -> Pro. Returns True when a tenant was updated."""
    session_obj = event["data"]["object"]
    tenant = _resolve_tenant_from_session(db, session_obj)
    if tenant is None:
        return False

    customer_id = _get(session_obj, "customer")
    if customer_id and not tenant.stripe_customer_id:
        tenant.stripe_customer_id = customer_id

    # `subscription` is normally the id string, but Stripe may expand it into a
    # shallow object depending on API version. Handle both.
    subscription = _get(session_obj, "subscription")
    if isinstance(subscription, str):
        sub_obj = {
            "id": subscription,
            "status": "active",
            "customer": customer_id,
            "metadata": {"tenant_id": str(tenant.id)},
        }
    else:
        sub_obj = {
            "id": _get(subscription, "id"),
            "status": _get(subscription, "status", "active"),
            "metadata": _get(subscription, "metadata") or {},
        }

    _apply_subscription_state(db, tenant, sub_obj, activate=True)
    return True


def handle_customer_subscription_updated(db: Session, event: stripe.Event) -> bool:
    """Sync plan/status for an existing subscription. Never touches other tenants."""
    sub_obj = event["data"]["object"]
    tenant = _resolve_tenant_from_subscription(db, sub_obj)
    if tenant is None:
        return False
    _apply_subscription_state(db, tenant, sub_obj, activate=True)
    return True


def handle_customer_subscription_deleted(db: Session, event: stripe.Event) -> bool:
    """Mark inactive. Historical rows are kept for auditability."""
    sub_obj = event["data"]["object"]
    tenant = _resolve_tenant_from_subscription(db, sub_obj)
    if tenant is None:
        return False
    _apply_subscription_state(db, tenant, sub_obj, activate=False)
    return True


HANDLERS = {
    "checkout.session.completed": handle_checkout_session_completed,
    "customer.subscription.updated": handle_customer_subscription_updated,
    "customer.subscription.deleted": handle_customer_subscription_deleted,
}


def already_processed(db: Session, stripe_event_id: str) -> bool:
    """Database check. This is the dedup source of truth, not an in-memory set."""
    row = db.execute(
        select(StripeEvent).where(StripeEvent.stripe_event_id == stripe_event_id)
    ).scalar_one_or_none()
    return row is not None


def process_event(db: Session, event: stripe.Event, raw_body: bytes) -> dict:
    """Apply a verified event exactly once.

    Returns a result dict describing what happened, for the response body and
    for evidence.
    """
    stripe_event_id = event["id"]
    event_type = event["type"]

    if already_processed(db, stripe_event_id):
        return {
            "status": "duplicate",
            "stripe_event_id": stripe_event_id,
            "event_type": event_type,
        }

    # Claim the event row before entering the business operation. The UNIQUE
    # constraint is the authority: under concurrent delivery only one session
    # can insert this row and proceed. If the handler fails, the transaction is
    # rolled back, releasing the claim so Stripe can retry safely.
    record = StripeEvent(
        stripe_event_id=stripe_event_id,
        event_type=event_type,
        payload=raw_body.decode("utf-8", errors="replace"),
        processed=False,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent duplicate won the race. Its row is authoritative.
        db.rollback()
        return {
            "status": "duplicate",
            "stripe_event_id": stripe_event_id,
            "event_type": event_type,
        }

    handler = HANDLERS.get(event_type)
    applied = handler(db, event) if handler is not None else False
    record.processed = applied
    record.processed_at = utcnow() if applied else None

    # The event row and the business change commit together. An exception in
    # the handler rolls back both, so Stripe's retry sees no partial state.
    try:
        db.commit()
    except IntegrityError:
        # Defensive fallback for a database that reports the unique conflict
        # at commit rather than flush.
        db.rollback()
        return {
            "status": "duplicate",
            "stripe_event_id": stripe_event_id,
            "event_type": event_type,
        }

    return {
        "status": "processed" if applied else "ignored",
        "stripe_event_id": stripe_event_id,
        "event_type": event_type,
    }
